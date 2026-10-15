"""The answer contract: the JSON the answer model must return, and every check it passes before
anything is shown (ANS-1, ANS-2, GRD-10..13).

The model is held to a strict JSON schema (response_format, non-streaming: Claude models on
Databricks support structured output only without streaming). It answers in blocks, and each block
carries the evidence for what it says: the SOURCE and the numbered lines (answering.evidence_units)
behind it. From that the agent cuts the excerpts out of the sources, writes the [n] markers itself,
and checks every quotation against the lines its own block cites, not against everything that was
retrieved. Nothing here guesses what a malformed reply meant: a reply that fails is sent back once
with what is wrong, and withheld if it fails again, with the reason recorded.

Failure categories (Checked.category): invalid_json, schema_validation, truncation,
evidence_failure. A gateway denial (policy_denial) is raised by the call itself and never gets here.
"""
from __future__ import annotations

import difflib
import json
import re
from dataclasses import dataclass, field

from . import guardrails as g
from . import sensitive
from .answering import MAX_CITED_LINES, evidence_units

STATUSES = ("answered", "out_of_scope", "no_source")
MAX_BLOCKS = 12
MAX_BLOCK_LINES = 12     # evidence lines one block may cite
MAX_ANSWER_LINES = 30    # evidence lines a whole answer may cite: the overall budget

INVALID_JSON, SCHEMA_VALIDATION, TRUNCATION, EVIDENCE = ("invalid_json", "schema_validation", "truncation",
                                                         "evidence_failure")

_EVIDENCE = {"type": "object", "additionalProperties": False, "required": ["source", "lines"],
             "properties": {"source": {"type": "integer"}, "lines": {"type": "array", "items": {"type": "integer"}}}}
SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["status", "blocks", "conflicts"],
    "properties": {
        "status": {"type": "string", "enum": list(STATUSES)},
        "blocks": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["text", "evidence"],
            "properties": {"text": {"type": "string"}, "evidence": {"type": "array", "items": _EVIDENCE}}}},
        "conflicts": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["sources", "note"],
            "properties": {"sources": {"type": "array", "items": {"type": "integer"}}, "note": {"type": "string"}}}},
    },
}


def response_format() -> dict:
    """What the answer request sends, so the model can only return this shape."""
    return {"type": "json_schema", "json_schema": {"name": "chatbot_answer", "schema": SCHEMA, "strict": True}}


# Models Databricks lists as not taking response_format. A service routed to one of them sends the
# schema as a function the model must call instead (structured_output: tool); the reply is then
# checked against the schema in code, exactly like a response_format reply.
NO_RESPONSE_FORMAT = ("databricks-claude-opus-5-5", "databricks-claude-sonnet-5-5", "databricks-claude-haiku-5-5",
                      "databricks-claude-fable-5-1")
TOOL_NAME = "submit_answer"
MODES = ("response_format", "tool")


def request_options(mode: str) -> dict:
    """The request arguments that hold the model to the schema, for a service's structured_output."""
    if mode == "tool":
        return {"tools": [{"type": "function", "function": {
                    "name": TOOL_NAME, "description": "Submit the reply in the required form.", "parameters": SCHEMA}}],
                "tool_choice": {"type": "function", "function": {"name": TOOL_NAME}}}
    return {"response_format": response_format()}


def reply_of(resp) -> tuple[str, str | None]:
    """The reply text and finish reason of a chat completion: the submit_answer call's arguments
    when the model called it, otherwise its message (which then fails the checks, as it should)."""
    choice = resp.choices[0]
    calls = getattr(choice.message, "tool_calls", None) or []
    text = calls[0].function.arguments if calls else (choice.message.content or "")
    return text or "", getattr(choice, "finish_reason", None)


def output_mode_problems(services: dict) -> list[str]:
    """Answer services set to response_format but routed (primary or fallback) to a model that
    doesn't take it, or set to something that isn't a mode."""
    problems = []
    for label, spec in (services or {}).items():
        mode = spec.get("structured_output", "response_format")
        routed = [spec.get("model", "")] + list(spec.get("fallbacks") or [])
        if mode not in MODES:
            problems.append(f"answer service '{label}': structured_output must be one of {', '.join(MODES)}")
        elif mode == "response_format":
            bad = [m for m in routed if any(str(m).endswith(x) for x in NO_RESPONSE_FORMAT)]
            if bad:
                problems.append(f"answer service '{label}' routes to {', '.join(bad)}, which Databricks doesn't "
                                "support response_format for: set its structured_output to tool (Admin › Platform)")
    return problems


@dataclass
class Checked:
    ok: bool
    category: str = ""                                   # why it failed: one of the categories above
    kinds: list[str] = field(default_factory=list)       # the finer causes, for counting
    problems: list[str] = field(default_factory=list)    # what to fix, written by the application
    status: str = ""
    blocks: list[dict] = field(default_factory=list)     # {"text", "cites": [index into citations]}
    citations: list[dict] = field(default_factory=list)  # {"source", "lines", "excerpt"}
    conflicts: list[dict] = field(default_factory=list)

    @property
    def answer(self) -> str:
        return render_text(self.blocks, self.citations)


def _fail(category: str, kind: str, *problems: str) -> Checked:
    return Checked(False, category, [kind], list(problems))


# Decoding ----------------------------------------------------------------------------------------
def decode(raw: str, finish_reason: str | None) -> tuple[dict | None, Checked | None]:
    """The reply as a dictionary that has the contract's shape, or the failure. A reply cut off by
    the length limit is a truncation, whatever is in it."""
    if finish_reason == "length":
        return None, _fail(TRUNCATION, "truncated", "Your reply was cut off at the length limit. Answer in fewer, "
                           "shorter blocks.")
    try:
        obj = json.loads(raw or "")
    except ValueError as e:
        return None, _fail(INVALID_JSON, "invalid_json", f"Your reply was not valid JSON ({getattr(e, 'msg', e)}). "
                           "Reply with one JSON object in the required form.")
    problems = schema_problems(obj)
    if problems:
        return None, _fail(SCHEMA_VALIDATION, "schema", *problems)
    return obj, None


def _ints(value) -> bool:
    return isinstance(value, list) and all(isinstance(v, int) and not isinstance(v, bool) for v in value)


def schema_problems(obj) -> list[str]:
    """Every way the decoded reply breaks the contract: fields and their types, the allowed
    statuses, the evidence structure, and what each status requires."""
    if not isinstance(obj, dict):
        return ["The reply must be one JSON object."]
    problems = [f'"{k}" is missing.' for k in ("status", "blocks", "conflicts") if k not in obj]
    problems += [f'"{k}" is not part of the form.' for k in obj if k not in ("status", "blocks", "conflicts")]
    status, blocks, conflicts = obj.get("status"), obj.get("blocks"), obj.get("conflicts")
    if "status" in obj and status not in STATUSES:
        problems.append(f'"status" must be one of {", ".join(STATUSES)}.')
    if "blocks" in obj and not isinstance(blocks, list):
        problems.append('"blocks" must be a list.')
    if "conflicts" in obj and not isinstance(conflicts, list):
        problems.append('"conflicts" must be a list.')
    if problems:
        return problems
    for i, b in enumerate(blocks, 1):
        if not isinstance(b, dict) or set(b) != {"text", "evidence"}:
            problems.append(f'Block {i} must have exactly "text" and "evidence".')
        elif not isinstance(b["text"], str) or not b["text"].strip():
            problems.append(f"Block {i} has no text.")
        elif not isinstance(b["evidence"], list) or not all(
                isinstance(e, dict) and set(e) == {"source", "lines"} and isinstance(e["source"], int)
                and not isinstance(e["source"], bool) and _ints(e["lines"]) for e in b["evidence"]):
            problems.append(f'Block {i}: each evidence entry is {{"source": <number>, "lines": [<numbers>]}}.')
    for i, c in enumerate(conflicts, 1):
        if not isinstance(c, dict) or not _ints(c.get("sources")) or not isinstance(c.get("note"), str):
            problems.append(f'Conflict {i} must be {{"sources": [<numbers>], "note": "..."}}.')
    if problems:
        return problems
    if status == "answered":
        if not blocks:
            problems.append('An "answered" reply needs at least one block.')
        if len(blocks) > MAX_BLOCKS:
            problems.append(f"Use at most {MAX_BLOCKS} blocks.")
        problems += [f"Block {i} has no evidence: every block needs the source lines behind it."
                     for i, b in enumerate(blocks, 1) if not b["evidence"]]
    elif blocks or conflicts:
        problems.append(f'A "{status}" reply has empty "blocks" and "conflicts".')
    return problems


# Quotations --------------------------------------------------------------------------------------
def quotations(text: str) -> tuple[list[str], bool]:
    """Every quotation in `text`, in order, and whether its quotation marks pair up. Curly marks
    pair as “ ”, straight double marks pair in turn, so a short label such as "Edit" or an amount
    such as "$1,000" is a quotation like any other, and the marks after it pair correctly."""
    found, i, balanced = [], 0, True
    while i < len(text):
        ch = text[i]
        close = "”" if ch == "“" else '"' if ch == '"' else None
        if close:
            j = text.find(close, i + 1)
            if j < 0:
                return found, False
            found.append(text[i + 1:j])
            i = j + 1
            continue
        if ch == "”":
            balanced = False
        i += 1
    return found, balanced


def _in(quote: str, text: str) -> bool:
    q = g.norm(quote).strip(" .,;:!?")
    return bool(q) and q in g.norm(text)


# Evidence ----------------------------------------------------------------------------------------
def resolve_evidence(evidence: list[dict], texts: list[str]) -> tuple[list[dict], list[str]]:
    """A block's evidence as spans {"source", "lines", "excerpt"}: the lines of each source, sorted,
    split into runs of consecutive lines, and a run longer than MAX_CITED_LINES split into pieces
    of that size. Nothing is dropped; each excerpt is cut out of the source, so it is verbatim."""
    spans, problems = [], []
    for e in evidence:
        n = e["source"]
        if not 1 <= n <= len(texts):
            problems.append(f"There is no SOURCE [{n}]; the sources are numbered 1 to {len(texts)}.")
            continue
        units = evidence_units(texts[n - 1])
        ks = sorted(set(e["lines"]))
        bad = [k for k in ks if not 1 <= k <= len(units)]
        if not ks or bad:
            problems.append(f"SOURCE [{n}] has lines L1 to L{len(units)}"
                            + (f"; it has no line {', '.join(f'L{k}' for k in bad)}." if bad else "; name at least one."))
            continue
        runs = [[ks[0]]]
        for k in ks[1:]:
            if k == runs[-1][-1] + 1 and len(runs[-1]) < MAX_CITED_LINES:
                runs[-1].append(k)
            else:
                runs.append([k])
        for run in runs:
            a, b = units[run[0] - 1][0], units[run[-1] - 1][1]
            spans.append({"source": n, "lines": run, "excerpt": texts[n - 1][a:b]})
    return spans, problems


def _where(quote: str, texts: list[str]) -> str:
    """Where a quotation really is, or the closest line, as a reference (never the line's text)."""
    lines = [(n, k, t[a:b]) for n, t in enumerate(texts, 1) for k, (a, b) in enumerate(evidence_units(t), 1)]
    exact = [f"SOURCE [{n}] L{k}" for n, k, line in lines if _in(quote, line)]
    if exact:
        return f"Those words are in {exact[0]}: add that line to this block's evidence, or don't quote them."
    best = max(lines, key=lambda x: difflib.SequenceMatcher(None, quote.lower(), x[2].lower()).ratio(), default=None)
    if best and difflib.SequenceMatcher(None, quote.lower(), best[2].lower()).ratio() >= 0.6:
        return f"The closest line is SOURCE [{best[0]}] L{best[1]}: quote its exact words, or don't quote."
    return "No line has those words: don't put them in quotation marks."


def _fragment(quote: str) -> str:
    words = quote.split()
    return "“" + " ".join(words[:6]) + ("…" if len(words) > 6 else "") + "”"


def _digits(phone: str) -> str:
    d = "".join(ch for ch in phone if ch.isdigit())
    return d[1:] if len(d) == 11 and d.startswith("1") else d


def contact_problems(text: str, excerpts: list[str], contacts: dict | None) -> list[tuple[str, str]]:
    """Email addresses and phone numbers in a block that it may not show, with what each is. Decided
    2026-10-15: one may appear when it is in the lines the block cites (so it comes from the
    documents), and the company's own (contacts: email_domains, phone_numbers) always may."""
    contacts = contacts or {}
    domains = {str(d).lower().lstrip("@") for d in contacts.get("email_domains") or []}
    phones = {_digits(str(p)) for p in contacts.get("phone_numbers") or []}
    cited = " ".join(excerpts)
    cited_emails = {e.lower() for e in sensitive.EMAIL.findall(cited)}
    cited_phones = {_digits(p) for p in sensitive.PHONE.findall(cited)}
    out = []
    for e in sensitive.EMAIL.findall(text):
        if e.lower().split("@")[-1] not in domains and e.lower() not in cited_emails:
            out.append((e, "email address"))
    for p in sensitive.PHONE.findall(text):
        if _digits(p) not in phones and _digits(p) not in cited_phones:
            out.append((p.strip(), "phone number"))
    return out


def check(obj: dict, texts: list[str], system_prompt: str, *, limit_quotes: bool = False,
          max_quote_words: int = 60, contacts: dict | None = None) -> Checked:
    """Every check a decoded reply must pass. For an answer: each block's evidence resolves within
    the budget, every quotation in a block is exact words from the lines that block cites, every
    email address and phone number is in those lines or is the company's own, the quotation marks
    pair up, no invented links, no prompt leak, no gateway placeholder, and (for chatbots that
    limit quoting) no quotation longer than max_quote_words."""
    status = obj["status"]
    if status != "answered":
        return Checked(True, status=status)
    citations, blocks, problems, kinds, used = [], [], [], [], 0
    for i, b in enumerate(obj["blocks"], 1):
        spans, span_problems = resolve_evidence(b["evidence"], texts)
        if span_problems:
            kinds.append("citation_lines")
            problems += [f"Block {i}: {p}" for p in span_problems]
        lines = sum(len(s["lines"]) for s in spans)
        used += lines
        if lines > MAX_BLOCK_LINES:
            kinds.append("evidence_budget")
            problems.append(f"Block {i} cites {lines} lines; one block may cite at most {MAX_BLOCK_LINES}. Split it "
                            "into blocks that each say less.")
        text = re.sub(r"\s*\[\d+\]", "", b["text"]).strip()   # [n] markers are written by the application
        found, balanced = quotations(text)
        if not balanced:
            kinds.append("unbalanced_quotes")
            problems.append(f"Block {i} has a quotation mark without its partner.")
        for q in found:
            if not q.strip():
                continue
            if not any(_in(q, s["excerpt"]) for s in spans):
                kinds.append("quote_not_in_evidence")
                problems.append(f"Block {i}: the quoted words {_fragment(q)} are not in the lines this block cites. "
                                + _where(q, texts))
            elif limit_quotes and len(q.split()) > max_quote_words:
                kinds.append("quote_too_long")
                problems.append(f"Block {i}: a quotation is {len(q.split())} words; keep each under "
                                f"{max_quote_words}.")
        for value, what in contact_problems(text, [s["excerpt"] for s in spans], contacts):
            kinds.append("contact_not_in_evidence")
            where = next((f"SOURCE [{n}]" for n, t in enumerate(texts, 1) if value.lower() in t.lower()), "")
            problems.append(f"Block {i} shows the {what} {value}, which is not in the lines it cites"
                            + (f"; it is in {where}: cite that line, or leave it out." if where
                               else " or anywhere in the documents: leave it out."))
        index = []
        for s in spans:
            key = (s["source"], tuple(s["lines"]))
            at = next((j for j, c in enumerate(citations) if (c["source"], tuple(c["lines"])) == key), None)
            if at is None:
                citations.append(s)
                at = len(citations) - 1
            index.append(at)
        blocks.append({"text": text, "cites": index})
    if used > MAX_ANSWER_LINES:
        kinds.append("evidence_budget")
        problems.append(f"The answer cites {used} lines in all; the limit is {MAX_ANSWER_LINES}. Cite only the "
                        "lines that support what each block says.")
    answer = render_text(blocks, citations)
    links = g.invented_links(answer, texts)
    if links:
        kinds.append("no_invented_links")
        problems.append("Remove links that are not in the sources: " + ", ".join(links))
    if g.leaks_prompt(answer, system_prompt):
        kinds.append("prompt_leak")
        problems.append("Leave out the application's own instructions.")
    placeholders = g.gateway_placeholders(answer, texts)
    if placeholders:
        kinds.append("gateway_redaction")
        problems.append(f"The answer contains {', '.join(placeholders)}, which the gateway put in place of text; "
                        "leave that part out.")
    conflicts = [c for c in obj["conflicts"] if c["sources"]]
    if problems:
        return Checked(False, EVIDENCE, sorted(set(kinds)), problems, status, blocks, citations, conflicts)
    return Checked(True, "", [], [], status, blocks, citations, conflicts)


def render_text(blocks: list[dict], citations: list[dict]) -> str:
    """The answer as shown: each block followed by the markers of the sources it cites."""
    out = []
    for b in blocks:
        sources = sorted({citations[j]["source"] for j in b["cites"]})
        out.append(b["text"] + (" " + "".join(f"[{n}]" for n in sources) if sources else ""))
    return " ".join(out)


def verify_final(blocks: list[dict], excerpts: list[str]) -> list[str]:
    """After redaction and shortening, check again what will actually be shown: every quotation of
    every block still within its own block's excerpts (`excerpts[j]` for citation j, in full). A
    change made after the checks (a redacted number inside a quotation, say) must not slip through."""
    problems = []
    for i, b in enumerate(blocks, 1):
        found, balanced = quotations(b["text"])
        if not balanced:
            problems.append(f"block {i}: unbalanced quotation marks after redaction")
        problems += [f"block {i}: quotation {_fragment(q)} changed after the checks" for q in found
                     if q.strip() and not any(_in(q, excerpts[j]) for j in b["cites"])]
    return problems


# Repair --------------------------------------------------------------------------------------------
HOW_EVIDENCE_WORKS = (
    'Each block\'s "evidence" lists {"source": n, "lines": [...]}: the SOURCE number and the numbers of its lines '
    "(L1, L2, ...) that support exactly what the block says. The lines need not be next to each other. A block may "
    f"cite at most {MAX_BLOCK_LINES} lines and the whole answer {MAX_ANSWER_LINES}. Anything in quotation marks must "
    "be exact words from a line that same block cites. Don't write [n] markers; they are added for you.")


def repair_note(failed: Checked) -> str:
    """What the second attempt is told, added to the system message. Written by the application:
    the problems by block and source line, never the earlier reply itself."""
    lines = [f"YOUR PREVIOUS REPLY FAILED THE APPLICATION'S CHECKS ({failed.category.replace('_', ' ')}). "
             "Reply again in the same JSON form and fix exactly these problems:"]
    lines += [f"- {p}" for p in failed.problems[:12]]
    if failed.category == EVIDENCE:
        lines.append(HOW_EVIDENCE_WORKS)
    return "\n".join(lines)


def answer_with_checks(call, messages: list[dict], texts: list[str], system_prompt: str, *, attempts: int = 2,
                       limit_quotes: bool = False, max_quote_words: int = 60,
                       contacts: dict | None = None) -> tuple[Checked, list[Checked]]:
    """Ask, check, and ask once more with what is wrong; returns the last result and every failed
    attempt before it. `call(messages, attempt)` returns (reply text, finish_reason) and raises for a
    gateway denial or a failed request: those are not retried here."""
    result, note, failures = None, "", []
    for attempt in range(1, attempts + 1):
        msgs = messages if not note else [
            {"role": "system", "content": messages[0]["content"] + "\n\n" + note}] + messages[1:]
        raw, finish = call(msgs, attempt)
        obj, result = decode(raw, finish)
        if obj is not None:
            result = check(obj, texts, system_prompt, limit_quotes=limit_quotes, max_quote_words=max_quote_words,
                           contacts=contacts)
        if result.ok:
            return result, failures
        failures.append(result)
        note = repair_note(result)
    return result, failures[:-1]
