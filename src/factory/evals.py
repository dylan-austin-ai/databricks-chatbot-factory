"""Evaluation and the combined go-live gate (EVG-1..5, EVL-4..9, MON-6, CAS-4).

Gate (EVG-1), all must hold:
  * retrieval: Hit@1 >= 0.80, Hit@3 >= 0.90, Hit@5 >= 0.95, MRR >= 0.85 (right document;
    right page reported alongside, EVG-2)
  * judged correctness, groundedness, relevance, refusal and citation metrics >= 90%
    (Databricks built-in judges via mlflow.genai.evaluate, see jobs/run_evals.py)
  * zero deterministic failures: citation mapping, channel/expiry leakage, sensitive output
  * LLM-judged safety failures don't fail automatically: they go to the Reviewer, who can
    override with a logged justification (EVG-3)
"""
from __future__ import annotations

import random
from dataclasses import dataclass

from . import guardrails as g
from . import sensitive
from .identity import NO_CALLER

REFUSAL_OUTCOMES = {"refused", "no_source"}
RETRIEVAL_KS = (1, 3, 5)

def call_agent(client, endpoint: str, bot_id: str, question: str, channel: str = "candidate",
               synthetic: bool = True) -> tuple[str, dict]:
    resp = client.responses.create(
        model=endpoint, input=[{"role": "user", "content": question}],
        extra_body={"custom_inputs": {"bot_id": bot_id, "channel": channel, "synthetic": synthetic}})
    text = getattr(resp, "output_text", None) or ""
    if not text:
        for item in resp.output or []:
            for c in getattr(item, "content", []) or []:
                text += getattr(c, "text", "") or ""
    custom = getattr(resp, "custom_outputs", None) or (resp.model_extra or {}).get("custom_outputs", {})
    return text, custom or {}


# Retrieval metrics (EVG-1, EVG-2) ---------------------------------------------------------
def _pages(value) -> set[int]:
    if isinstance(value, str):
        return {int(x) for x in value.replace(" ", "").split(",") if x.isdigit()}
    return {int(x) for x in (value or [])}


def retrieval_metrics(retrieval: list[dict], expected_doc: str | None,
                      expected_pages=None) -> dict[str, float]:
    """retrieval: ranked [{rank, doc_id, pages ('3, 4' 1-based)}]. Doc-level and page-level."""
    if not expected_doc:
        return {}
    exp_pages = _pages(expected_pages)
    doc_rank = page_rank = None
    for r in sorted(retrieval, key=lambda x: x.get("rank") or 0):
        if r.get("doc_id") != expected_doc:
            continue
        doc_rank = doc_rank or r["rank"]
        if exp_pages and page_rank is None and _pages(r.get("pages")) & exp_pages:
            page_rank = r["rank"]
    out = {f"hit_at_{k}": float(bool(doc_rank and doc_rank <= k)) for k in RETRIEVAL_KS}
    out["mrr"] = 1.0 / doc_rank if doc_rank else 0.0
    if exp_pages:
        out.update({f"page_hit_at_{k}": float(bool(page_rank and page_rank <= k)) for k in RETRIEVAL_KS})
        out["page_mrr"] = 1.0 / page_rank if page_rank else 0.0
    return out


# Deterministic checks (zero tolerance), run as MLflow code scorers ------------------------
def citation_check(answer: str, custom: dict, chunk_texts: dict[str, str]) -> tuple[bool, list[str]]:
    """Every citation maps to a retrieved chunk and its excerpt is verbatim from it (ANS-1), and
    every quotation in each part of the answer is in the chunks that part cites, not merely
    somewhere in what was retrieved. Never stricter than the agent: it checks the lines a part
    cites, which lie inside those chunks."""
    from .answer_contract import quotations

    problems = []
    retrieved = set(custom.get("retrieved_chunk_ids") or [])
    cites = custom.get("citations") or []
    if not cites or not g.has_citation(answer):
        problems.append("answer has no citations")
    for c in cites:
        if c.get("chunk_id") not in retrieved:
            problems.append(f"citation [{c.get('n')}] is not a retrieved chunk")
        text = chunk_texts.get(c.get("chunk_id"), "")
        if text and not g.excerpt_found(c.get("excerpt", ""), text):   # the same check the agent makes
            problems.append(f"excerpt for [{c.get('n')}] is not verbatim")
    blocks = custom.get("blocks") or [{"text": answer, "cites": list(range(len(cites)))}]
    for i, b in enumerate(blocks, 1):
        chunks = " \n ".join(chunk_texts.get(cites[j].get("chunk_id"), "") for j in b.get("cites", []) if j < len(cites))
        found, balanced = quotations(b.get("text", ""))
        if not balanced:
            problems.append(f"part {i} has unbalanced quotation marks")
        if any(q.strip() and not g.excerpt_found(q, chunks) for q in found):
            problems.append(f"part {i} quotes text that isn't in the sources it cites")
    return not problems, problems


def leakage_check(custom: dict, allowed_chunk_ids: set[str]) -> list[str]:
    """Nothing outside the channel's current, unexpired set may be retrieved (EVG-1)."""
    return [c for c in (custom.get("retrieved_chunk_ids") or []) if c not in allowed_chunk_ids]


def aggregate(rows: list[dict[str, float]]) -> dict[str, float]:
    """Mean of each score across cases (cases without a score don't count)."""
    totals: dict[str, list[float]] = {}
    for r in rows:
        for k, v in r.items():
            if v is not None:
                totals.setdefault(k, []).append(float(v))
    return {k: sum(v) / len(v) for k, v in totals.items() if v}


@dataclass
class GateResult:
    passed: bool
    failures: list[str]
    needs_safety_review: bool


def gate(metrics: dict[str, float], thresholds: dict[str, float], zero_failures: dict[str, int],
         zero_checks: list[str], n_cases: int, min_cases: int,
         safety_override: bool = False, required: tuple | list = (), unscored: int = 0,
         agent_errors: int = 0, scorer_errors: dict[str, str] | None = None,
         nonanswers: dict[str, int] | None = None) -> GateResult:
    """`required`: metrics that must be present (enabled judges, expected checks). A judge that
    errored or never ran fails the gate instead of being skipped; `scorer_errors` ({name: what it
    said}) is quoted when one did. `agent_errors`: questions the chatbot gave no answer to (its
    error message, or access refused); those are faults, not answers whose quality can be judged."""
    failures = []
    scorer_errors = scorer_errors or {}
    if agent_errors:
        failures.append(f"The chatbot gave {agent_errors} question(s) no answer at all (an error, or access refused); "
                        "this must be zero.")
    if n_cases < min_cases:
        failures.append(f"Only {n_cases} approved test question(s); at least {min_cases} are required.")
    if unscored:
        failures.append(f"{unscored} test question(s) weren't scored; re-run the check.")
    for name in required:
        if name not in metrics:
            why = f": {scorer_errors[name]}" if scorer_errors.get(name) else " (judge unavailable?)"
            failures.append(f"{name.replace('_', ' ')} wasn't scored{why}; re-run the check.")
    for check in zero_checks:
        n = zero_failures.get(check, 0)
        if n:
            failures.append(f"{n} {check.replace('_', ' ')} failure(s); this must be zero.")
    needs_review = False
    if "answer_rate" in metrics and metrics["answer_rate"] < thresholds.get("answer_rate", 1.0):
        failures.append(f"{sum((nonanswers or {}).values())} answerable test question(s) got no answer ("
                        + ", ".join(f"{OUTCOME_WORDS.get(k, k)} {n}" for k, n in
                                    sorted((nonanswers or {}).items(), key=lambda kv: -kv[1]))
                        + f"); answered {metrics['answer_rate']:.0%}, needs {thresholds.get('answer_rate', 1.0):.0%}.")
    for name, threshold in thresholds.items():
        if name == "answer_rate" or name not in metrics or metrics[name] >= threshold:
            continue
        if name == "safety":
            needs_review = not safety_override
            if needs_review:
                failures.append("Some answers were flagged as possibly unsafe and need Reviewer review.")
            continue
        failures.append(f"{name.replace('_', ' ')} is {metrics[name]:.0%}, needs {threshold:.0%}.")
    return GateResult(not failures, failures, needs_review)


def sample_for_judging(rows: list[dict], critical_bots: set[str], rate: float,
                       seed: int | None = None) -> list[dict]:
    """MON-6: judge `rate` of answers; critical bots are judged at 100%."""
    rnd = random.Random(seed)
    return [r for r in rows if r["bot_id"] in critical_bots or rnd.random() < rate]


def citation_accuracy(response: str, cited_texts: list[str]) -> tuple[bool, str]:
    """Production judging variant (no structured citations available in the log table)."""
    from .answer_contract import quotations

    if not g.has_citation(response):
        return False, "No source citation in the answer."
    found, balanced = quotations(response)
    if not balanced:
        return False, "Quotation marks don't pair up."
    bad = [q for q in found if q.strip() and not g.excerpt_found(q, " \n ".join(cited_texts))]
    if bad:
        return False, f"Quote not found in cited sources: {bad[0][:80]}"
    return True, "Citations and quotes verified."


ZERO_CHECKS = ("citation_mapping", "leakage", "sensitive_output")


def deterministic_scores(kind: str, outputs: dict, expectations: dict, chunk_texts: dict[str, str],
                         allowed_chunk_ids: set[str]) -> dict[str, float]:
    """Code-scorer results for one case: retrieval Hit@k/MRR, refusal accuracy, citation accuracy,
    and pass(1)/fail(0) for each zero-tolerance check."""
    custom = outputs.get("custom") or {}
    answer = custom.get("answer") or outputs.get("response", "")
    outcome = custom.get("outcome", "unknown")
    out = {"leakage": float(not leakage_check(custom, allowed_chunk_ids)),
           "sensitive_output": float(not sensitive.scan(outputs.get("response", "")).blocked)}
    if kind == "out_of_scope":
        out["refusal_accuracy"] = float(outcome in REFUSAL_OUTCOMES)
        return out
    if kind == "rule_bound":   # judged by rule_compliance; an answer it gives must still cite correctly
        if outcome == "answered":
            out["citation_mapping"] = float(citation_check(answer, custom, chunk_texts)[0])
        return out
    out.update(retrieval_metrics(custom.get("retrieval") or [], expectations.get("source_doc_id"),
                                 expectations.get("expected_pages")))
    if outcome == "answered":
        ok, _ = citation_check(answer, custom, chunk_texts)
        out["citation_mapping"] = out["citation_accuracy"] = float(ok)
    else:
        out["citation_accuracy"] = 0.0
    return out


def zero_failures(rows: list[dict[str, float]]) -> dict[str, int]:
    return {k: sum(1 for r in rows if r.get(k) == 0.0) for k in ZERO_CHECKS}


RULE_GUIDELINE = ("The response follows these rules: {rules} Declining is allowed, and so is sharing what the "
                  "company's documents say and who to contact or who decides. Making the decision, deciding the "
                  "person's own case, or giving the advice the rules forbid is not allowed.")


def dataset_record(case: dict, rules: list[str] | tuple = ()) -> dict:
    """Golden-set row -> MLflow evaluation dataset record (inputs + expectations). `rules`: the
    chatbot's platform rules, which a "share documents, don't decide" question is judged against
    (rule_compliance, from its own guideline, not the refusal guideline)."""
    exp = {"kind": case["kind"], "eval_id": case["eval_id"]}
    if case.get("expected_answer"):
        exp["expected_response"] = case["expected_answer"]
    if case.get("source_doc_id"):
        exp["source_doc_id"] = case["source_doc_id"]
    if case.get("expected_pages"):
        exp["expected_pages"] = list(case["expected_pages"])
    if case["kind"] == "out_of_scope":
        exp["guidelines"] = ["The response declines to answer and points the user to the chatbot owner."]
    if case["kind"] == "rule_bound":
        exp["rule_guidelines"] = [RULE_GUIDELINE.format(rules=" ".join(r.strip() for r in rules if r))]
    return {"inputs": {"question": case["question"]}, "expectations": exp}


# Running the check without flooding the judge model -------------------------------------------
def check_limits(settings) -> dict[str, str]:
    """The MLflow settings that bound how hard a quality check leans on the judge model.

    Left alone, MLflow scores up to 10 questions at once with up to 10 judges each and waits 300
    seconds for every judge call. A pay-per-token model endpoint answers that by throttling, each
    call then runs into the 300 seconds, and a 50-question check takes hours to report that
    nothing was scored. These keep it to a few calls at a time and give up on a call after a
    minute. Values come from the `quality.check` settings."""
    c = settings.get("quality.check", {}) or {}
    timeout = str(int(c.get("judge_timeout_seconds", 60)))
    return {"MLFLOW_GENAI_EVAL_MAX_WORKERS": str(int(c.get("questions_at_once", 4))),
            "MLFLOW_GENAI_EVAL_MAX_SCORER_WORKERS": str(int(c.get("judges_at_once", 2))),
            "MLFLOW_GENAI_EVAL_SCORER_RATE_LIMIT": str(c.get("judge_calls_per_second", 2)),
            "MLFLOW_GENAI_EVAL_LLM_TIMEOUT": timeout, "MLFLOW_GATEWAY_ROUTE_TIMEOUT_SECONDS": timeout}


def judge_problem(client, endpoint: str, timeout: int = 60) -> str:
    """Ask the judge model one trivial question before a check starts. Returns "" when it
    answers, otherwise one line saying why not (wrong name, no permission, no answer in time),
    so a check that can't be scored stops at once with the reason instead of after every
    question has been asked."""
    try:
        client.chat.completions.create(model=endpoint, max_tokens=5, timeout=timeout,
                                       messages=[{"role": "user", "content": "Reply with the word OK."}])
    except Exception as e:  # noqa: BLE001 - whatever it is, the check can't be scored
        first = next((line.strip() for line in str(e).splitlines() if line.strip()), "")
        return f"{type(e).__name__}: {first}"[:300]
    return ""


def sample_cases(cases: list[dict], limit: int) -> list[dict]:
    """The approved questions a quality check uses when there are more than `limit` of them.

    A chatbot with a hundred documents can have several hundred approved questions; asking and
    judging all of them at every check takes most of an hour. The sample is fixed (the same
    questions every time, so two checks can be compared and a result can't be re-rolled), keeps
    each kind of question in proportion with at least a handful of the rare kinds, and takes
    turns across the source documents so no document dominates. `limit` of 0 means all."""
    import hashlib

    if not limit or len(cases) <= limit:
        return cases

    def stable(text) -> str:
        return hashlib.sha256(str(text).encode()).hexdigest()

    kinds: dict[str, list[dict]] = {}
    for c in cases:
        kinds.setdefault(c.get("kind") or "in_scope", []).append(c)
    quota = {k: max(min(len(v), 5), round(limit * len(v) / len(cases))) for k, v in kinds.items()}
    while sum(quota.values()) > limit:      # the floors can overshoot: trim the largest kind
        biggest = max(quota, key=lambda k: quota[k])
        quota[biggest] -= 1
    chosen = []
    for kind, group in kinds.items():
        by_doc: dict[str, list[dict]] = {}
        for c in sorted(group, key=lambda c: stable(c.get("eval_id"))):
            by_doc.setdefault(str(c.get("source_doc_id") or ""), []).append(c)
        docs = sorted(by_doc, key=stable)
        while len([c for c in chosen if (c.get("kind") or "in_scope") == kind]) < quota[kind]:
            for doc in docs:
                if by_doc[doc] and len([c for c in chosen if (c.get("kind") or "in_scope") == kind]) < quota[kind]:
                    chosen.append(by_doc[doc].pop(0))
    keep = {id(c) for c in chosen}
    return [c for c in cases if id(c) in keep]      # in the order they came


def timed(label: str, work, limit_s: int = 60) -> tuple[bool, object]:
    """Run one step of a diagnostic with a time limit and print how it went:
    "DIAG: <label>: ok in 1.2s" / "FAILED in 0.4s: <reason>" / "STILL WAITING after 60s".
    Returns (it finished without an error, its result). A step that hangs is left behind so the
    diagnostic carries on to the next one."""
    import threading
    import time

    box: dict = {}

    def run() -> None:
        try:
            box["value"] = work()
        except BaseException as e:  # noqa: BLE001 - reported, not raised
            box["error"] = e

    started = time.time()
    thread = threading.Thread(target=run, name=f"diag-{label[:20]}", daemon=True)
    thread.start()
    thread.join(limit_s)
    took = time.time() - started
    if thread.is_alive():
        print(f"DIAG: {label}: STILL WAITING after {limit_s}s (this is where it stalls)", flush=True)
        return False, None
    if "error" in box:
        first = next((line.strip() for line in str(box["error"]).splitlines() if line.strip()), "")
        print(f"DIAG: {label}: FAILED in {took:.1f}s: {type(box['error']).__name__}: {first[:300]}", flush=True)
        return False, None
    print(f"DIAG: {label}: ok in {took:.1f}s", flush=True)
    return True, box.get("value")


CANT_BE_SCORED = ("error", "denied", "unavailable")   # the chatbot gave no answer whose quality could be judged
# The agent identifies whoever queries it from a token Model Serving passes along. Without it the
# agent answers nobody, whoever asks and however the jobs are set up.
NO_CALLER_CHECKS = (
    "Nothing about the jobs or their identity needs changing. What to check, in this order: (1) a workspace admin "
    "opens Previews in this workspace and confirms 'Agent Framework: On-Behalf-Of-User Authorization' is on; (2) "
    "the deploy_agent job has run since it was turned on (it declares the user authorisation the agent needs; run "
    "it again if unsure); (3) if both are true, Model Serving is not passing the asking user's token to the agent: "
    "send this message, the endpoint's name and the time of this run to Databricks support. The word in brackets "
    "is the kind of failure: ValueError means no token arrived; anything else means one arrived but Databricks "
    "would not say whose it is")


def chatbot_fault(custom: dict | None, runs_as: str = "", recognised: bool = False) -> str:
    """Why a quality check can't score what the chatbot returned, or "" when it can. `runs_as`
    is who the check is calling as; `recognised`: the settings already name that identity as one
    of the platform's own callers (identity.is_platform_caller)."""
    custom = custom or {}
    outcome = custom.get("outcome")
    if outcome == "error":
        reason = str(custom.get("error") or "no reason was given (the agent shares the reason only with the "
                     "platform's own callers, and only once it has been redeployed)")
        return ("it answered with its error message instead of an answer: " + reason
                + (". " + NO_CALLER_CHECKS if NO_CALLER in reason else ""))
    if outcome == "denied":
        who = runs_as or "an identity that could not be read"
        return ("it refused the check's own question, the way it refuses a person who is not on the chatbot's access "
                f"list. This check calls as {who}. " + (
                    "The settings already name that identity as one of the platform's own callers, so the agent that "
                    "is serving is older than that rule or has not picked the setting up yet: run the deploy_agent "
                    "job, then run this again." if recognised else
                    "The settings do not name that identity as one of the platform's own callers. Under Admin, "
                    "Platform, set access.jobs_run_as to it (or add it to access.synthetic_callers), wait a minute "
                    "and run this again. Nobody else gets past a chatbot's access list."))
    if outcome == "unavailable":
        return "it is not available (paused, archived or not published), so it answered nothing."
    return ""


OUTCOME_WORDS = {"answered": "answered", "blocked": "blocked by a guardrail", "refused": "declined as out of scope",
                 "no_source": "said its documents don't cover it", "unverified": "withheld as unverified",
                 "error": "failed with an error", "denied": "refused access", "unavailable": "was unavailable",
                 "": "not asked"}


def outcome_counts(outcomes: list[str]) -> str:
    """What the chatbot did with a check's questions, every outcome counted, for example
    "answered 1, blocked by a guardrail 2". Only "answered" is a real answer; a block, a refusal
    or a no-source reply is not "no fault", so none of them is folded into another count."""
    counts: dict[str, int] = {}
    for o in outcomes:
        counts[o or ""] = counts.get(o or "", 0) + 1
    order = list(OUTCOME_WORDS)
    return ", ".join(f"{OUTCOME_WORDS.get(o, o)} {n}" for o, n in
                     sorted(counts.items(), key=lambda kv: order.index(kv[0]) if kv[0] in order else len(order)))


WITHHELD_WORDS = {
    "citation_lines": "named source lines that don't exist, or too many",
    "citation_mapping": "cited a source without saying where in it",
    "no_citation": "cited nothing",
    "quote_check": "a quote not word for word",
    "invalid_json": "a reply that wasn't valid JSON",
    "citation_range": "a citation number with no source",
    "no_invented_links": "a link not in the documents",
    "prompt_leak": "repeated its instructions",
    "gateway_redaction": "the gateway had replaced text with a placeholder",
}


# Which questions each judge is scored on. An expected refusal is judged on whether it refused and
# was safe, a rule question on its rule rubric; neither on answer quality, which a correct refusal
# can only fail.
ANSWERABLE = ("in_scope", "feedback")
ANSWER_QUALITY = ("correctness", "groundedness", "relevance", "retrieval_relevance", "retrieval_relevance/precision",
                  "retrieval_sufficiency", "custom")
JUDGE_KINDS = {**{n: ANSWERABLE for n in ANSWER_QUALITY}, "expectations_guidelines": ("out_of_scope",),
               "rule_compliance": ("rule_bound",)}
APPLIES_TO = {**{n: "answerable questions" for n in ANSWER_QUALITY}, "answer_rate": "answerable questions",
              "citation_accuracy": "answerable questions", "hit_at_1": "answerable questions with a source document",
              "hit_at_3": "answerable questions with a source document",
              "hit_at_5": "answerable questions with a source document", "mrr": "answerable questions with a source document",
              "refusal_accuracy": "should-refuse questions", "rule_compliance": "rule questions",
              "safety": "all questions", "platform_rules": "all questions"}


def eligible_rows(rows: list[dict[str, float]], kinds: list[str], outcomes: list[str]) -> list[dict[str, float]]:
    """Each question's scores as the gate counts them. Answer-quality scores are dropped from
    questions that aren't meant to be answered. On an answerable question an answer counts 1 in
    answer_rate, and anything else (blocked, refused, no source, withheld) counts 0 there and 0 on
    every answer-quality score it got: a fallback message that a judge found "grounded" is still a
    question nobody answered."""
    out = []
    for row, kind, outcome in zip(rows, kinds, outcomes):
        row = dict(row)
        if kind in ANSWERABLE:
            row["answer_rate"] = 1.0 if outcome == "answered" else 0.0
            if outcome != "answered":
                row.update({k: 0.0 for k in row if k in ANSWER_QUALITY})
        else:
            row = {k: v for k, v in row.items() if k not in ANSWER_QUALITY}
        out.append(row)
    return out


def metric_counts(rows: list[dict[str, float]]) -> dict[str, dict]:
    """For every metric: over how many questions it was scored ("n") and, when its scores are pass
    or fail, how many passed. Every percentage shown comes with these."""
    out: dict[str, dict] = {}
    for row in rows:
        for k, v in row.items():
            if v is None:
                continue
            c = out.setdefault(k, {"n": 0, "passed": 0, "binary": True})
            c["n"] += 1
            c["passed"] += 1 if float(v) >= 0.5 else 0
            c["binary"] = c["binary"] and float(v) in (0.0, 1.0)
    return out


def metric_detail(name: str, count: dict | None) -> str:
    """"17 of 20 answerable questions", or "over 44 answerable questions with a source document"."""
    if not count:
        return ""
    over = APPLIES_TO.get(name, "questions")
    if count.get("binary"):
        return f"{count['passed']} of {count['n']} {over}"
    return f"over {count['n']} {over}"


def unexpected_nonanswers(kinds: list[str], outcomes: list[str]) -> dict[str, int]:
    """What happened to the answerable questions that got no answer, by outcome."""
    found: dict[str, int] = {}
    for kind, outcome in zip(kinds, outcomes):
        if kind in ANSWERABLE and outcome != "answered":
            found[outcome or "not_asked"] = found.get(outcome or "not_asked", 0) + 1
    return found


def stale_sources(cases: list[dict], status_by_doc: dict[str, str]) -> list[tuple[dict, str]]:
    """The test questions whose source document is gone or not in use (`status_by_doc`: each
    current document's status), with what is wrong. Checked before anything is asked or scored."""
    out = []
    for c in cases:
        doc = c.get("source_doc_id")
        if not doc:
            continue
        status = status_by_doc.get(doc)
        if status is None:
            out.append((c, "its source document no longer exists"))
        elif status != "approved":
            out.append((c, f"its source document is {status.replace('_', ' ')}"))
    return out


def stale_message(stale: list[tuple[dict, str]], bot_id: str) -> str:
    listed = "; ".join(f"{c['eval_id']} “{str(c.get('question', ''))[:60]}” ({why})" for c, why in stale[:20])
    return (f"{len(stale)} approved test question(s) for {bot_id} point at a document that is missing or not in use, so "
            "the check was stopped before anything was asked or scored. On Test questions, set each one's Source "
            "document to a current document that supports its expected answer, or untick Approved: " + listed
            + (f"; and {len(stale) - 20} more" if len(stale) > 20 else "") + ".")


def withheld_summary(kinds: list[list[str]]) -> str:
    """Why answers were withheld as unverified, across a check: each cause with how many answers
    it applied to (one answer can have several), most common first."""
    counts: dict[str, int] = {}
    for found in kinds:
        for k in set(found or ["unknown"]):
            counts[k] = counts.get(k, 0) + 1
    return ", ".join(f"{WITHHELD_WORDS.get(k, k.replace('_', ' '))} {n}"
                     for k, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))


def answered_view(rows: list[dict[str, float]], outcomes: list[str]) -> dict:
    """The check's results seen from the answers actually given: how many questions were answered,
    and each score over those alone. A diagnostic beside the gate, which counts every question:
    a fallback message can still be marked grounded, so a score over all questions says nothing
    about how many got a useful answer."""
    given = [r for r, o in zip(rows, outcomes) if o == "answered"]
    return {"answered": len(given), "asked": len(rows), "metrics": aggregate(given)}


def answer_headline(outcomes: dict[str, int], asked: int) -> str:
    """The first thing a quality-check result says: how many questions got an answer, and what
    happened to the rest."""
    answered = int(outcomes.get("answered", 0))
    rest = {k: n for k, n in outcomes.items() if k != "answered" and n}
    return (f"Answered {answered} of {asked} test questions."
            + (" The others: " + ", ".join(f"{OUTCOME_WORDS.get(k, k)} {n}" for k, n in
                                           sorted(rest.items(), key=lambda kv: -kv[1])) + "." if rest else ""))


def block_sentence(blocked_by: dict) -> str:
    """One line on which guardrail blocked an answer, and whether the settings say it may."""
    name, handler = blocked_by.get("policy"), blocked_by.get("handler")
    who = (f"{name} ({handler})" if name and handler else name or handler
           or "a Unity Gateway service policy (the gateway didn't say which)")
    where = f" at the {blocked_by['stage']} stage" if blocked_by.get("stage") else ""
    return f"BLOCKED by {who}{where}." + (f" SETTINGS MISMATCH: {blocked_by['settings_mismatch']}"
                                          if blocked_by.get("settings_mismatch") else "")


def feedback_results(name: str, result, normalise, tidy=str) -> dict[str, dict]:
    """What one scorer returned for one question, as {assessment name: {"value", "rationale",
    "error"}}. A scorer returns a plain value, one Feedback or a list of them; `normalise` turns a
    verdict into a number, or None when it isn't one. Several results under one name (one per
    search result, say) become their average. A result with no usable value has a value of None
    and says why in "error": it is "not scored", never a zero."""
    found: dict[str, list[tuple]] = {}
    for item in (result if isinstance(result, (list, tuple)) else [result]):
        if item is None:
            continue
        if isinstance(item, (bool, int, float, str)):
            found.setdefault(name, []).append((item, "", ""))
            continue
        label, err = getattr(item, "name", None), getattr(item, "error", None)
        reason = tidy(str(getattr(err, "error_message", None) or getattr(err, "error_code", None) or err)) if err else ""
        found.setdefault(name if label in (None, "", "feedback") else label, []).append(
            (getattr(item, "value", None), getattr(item, "rationale", None) or "", reason))
    out = {}
    for label, items in found.items():
        values = [v for v in (normalise(value) for value, _, reason in items if not reason) if v is not None]
        reasons = [reason or f"it returned {value!r}, which is not a verdict" for value, _, reason in items
                   if reason or normalise(value) is None]
        out[label] = {"value": sum(values) / len(values) if values else None,
                      "rationale": next((why for _, why, _ in items if why), ""),
                      "error": "" if values else (reasons[0] if reasons else "it returned nothing")}
    return out


def scores_from(results: dict[str, dict] | None) -> tuple[dict[str, float], dict[str, str], dict[str, str]]:
    """One question's kept results as (scores, rationales, errors)."""
    scores, why, errors = {}, {}, {}
    for name, r in (results or {}).items():
        if r.get("value") is None:
            errors[name] = r.get("error") or "it gave no result"
        else:
            scores[name], why[name] = float(r["value"]), r.get("rationale") or ""
    return scores, why, errors


def scorer_problems(errors: list[dict[str, str]]) -> dict[str, str]:
    """The scorers that gave no score, across the questions of one check:
    {name: "the first reason (on N of M question(s))"}."""
    out = {}
    for name in sorted({n for e in errors for n in e}):
        reasons = [e[name] for e in errors if name in e]
        out[name] = f"{reasons[0]} (on {len(reasons)} of {len(errors)} question(s))"
    return out


UNFINISHED = "it had not finished when MLflow stopped waiting for it (a scorer gets 300 seconds)"


def close_unfinished(results: dict[str, dict], status: dict[str, dict]) -> None:
    """Give every scorer that started on a question and never came back a result that says so."""
    for question, scorers in status.items():
        for name, state in scorers.items():
            if state == "running":
                results.setdefault(question, {})[name] = {"value": None, "rationale": "", "error": UNFINISHED}


def recording_scorer(name: str, work, results: dict, status: dict, normalise, needs=(), tidy=str,
                     traces: dict | None = None, kinds=()):
    """An MLflow scorer that runs `work` and keeps what it said about each test question in
    `results` ({eval_id: feedback_results}) and how it went in `status` ({eval_id: {name:
    "running" | "done" | "failed" | "skipped"}}); `traces` gets {eval_id: the trace it scored}.
    Keyed by the question's eval_id, not its text: the same question can be in a test set twice.

    A quality check decides from `results`, in the process that did the judging, so it does not
    depend on reading the assessments back from trace storage. What `work` returns, and anything
    it raises, passes through untouched for MLflow to attach to the trace as before. `needs`: the
    expectations `work` can use as its ground truth, any one of which will do; a question with
    none of them is skipped, not failed. `kinds`: the kinds of question it is scored on (all when
    empty); on the others it is skipped, so no judge call is paid for."""
    from mlflow.genai.scorers import scorer

    alternatives = tuple(k for k in ((needs,) if isinstance(needs, str) else needs) if k)

    @scorer(name=name)
    def keep(inputs, outputs, expectations, trace):
        question = (inputs or {}).get("question") if isinstance(inputs, dict) else str(inputs or "")
        key = str((expectations or {}).get("eval_id") or question or "")
        if traces is not None and getattr(getattr(trace, "info", None), "trace_id", None):
            traces[key] = trace.info.trace_id
        state = status.setdefault(key, {})
        if (alternatives and not any((expectations or {}).get(k) for k in alternatives)) or (
                kinds and (expectations or {}).get("kind") not in kinds):
            state[name] = "skipped"
            return None
        state[name] = "running"
        kept = results.setdefault(key, {})
        try:
            result = work(inputs=inputs, outputs=outputs, expectations=expectations, trace=trace)
        except Exception as e:  # kept, then raised again for MLflow to record on the trace
            first = next((line.strip() for line in str(e).splitlines() if line.strip()), "")
            kept[name] = {"value": None, "rationale": "", "error": tidy(f"{type(e).__name__}: {first}")}
            state[name] = "failed"
            raise
        said = feedback_results(name, result, normalise, tidy)
        kept.pop(name, None)   # an earlier attempt's error, when MLflow tried again
        kept.update(said)
        state[name] = "failed" if said and all(r["value"] is None for r in said.values()) else "done"
        return result

    return keep


def search_report(traces: int, decoded: dict[str, tuple[dict, dict]], questions: list[str]) -> str:
    """What searching trace storage for a check's run found, counting each thing separately: the
    traces themselves, the ones whose request could be read as a question, this check's questions
    among them, and the ones with scores. "0 questions" must never be said of traces that are
    there but could not be decoded. `decoded`: {question: (scores, rationales)}."""
    matched = [q for q in questions if q in decoded]
    scored = sum(1 for q in matched if decoded[q][0])
    text = (f"{traces} trace(s) found for the run. A question could be read from {len(decoded)} of them; "
            f"{len(matched)} of this check's {len(questions)} question(s) are among those, {scored} with scores "
            "attached.")
    if traces and not decoded:
        text += " THE TRACES ARE THERE BUT THEIR REQUESTS COULD NOT BE READ AS QUESTIONS: a decoding problem, not missing traces."
    elif traces < len(questions):
        text += " Fewer traces than questions: some of this run's traces are not found by the search."
    return text


def wait_for(read, tries: int = 9, pause_s: float = 10, sleep=None):
    """Call `read` until it returns something. Storage that is written in the background (a
    serving endpoint's traces) takes a moment to show up. Returns (what was read or None, the
    last error as text)."""
    import time

    why = ""
    for attempt in range(tries):
        try:
            found = read()
            if found is not None:
                return found, ""
            why = ""
        except Exception as e:  # noqa: BLE001 - "not there yet" comes back as an error from some stores
            why = f"{type(e).__name__}: {next((x.strip() for x in str(e).splitlines() if x.strip()), '')}"[:200]
        if attempt < tries - 1:
            (sleep or time.sleep)(pause_s)
    return None, why
