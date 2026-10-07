"""Trace flow (OBS-18, OBS-19): the steps every answer goes through, built from MLflow trace spans.

`aggregate` turns many traces into one typical flow (order, depth, p50/p95, how often each step
runs, average cost); `tree` lays out a single trace. Spans are plain dicts so this unit-tests
without MLflow: name, type, span_id, parent_id, start_ns, end_ns, attributes.
"""
from __future__ import annotations

import re
from collections import defaultdict
from statistics import median

# Plain-English description and Databricks feature behind each step (shown on click).
SPANS: dict[str, tuple[str, str]] = {
    "chatbot.answer": ("The whole answer: every step below, start to finish, for one question.", "Model Serving"),
    "config.load": ("Reads this chatbot's settings (answer style, rules, search method) from a cached file. "
                    "No database call.", "Unity Catalog volume"),
    "identity.resolve": ("Works out who is asking: the signed-in person, or the user a trusted app vouches for.",
                         "Model Serving on-behalf-of authentication"),
    "access.check": ("Checks the person is in an allowed group. Cached for 15 minutes, so most checks take a "
                     "few milliseconds.", "Unity Catalog permissions"),
    "history.select": ("Picks the earlier turns of the conversation that fit, so follow-up questions make sense.",
                       "Agent memory (managed sessions)"),
    "query.rewrite": ("For follow-up questions only: rewrites them into a full question before searching.",
                      "Unity Gateway"),
    "retrieve": ("Finds the sections of your documents most likely to answer the question.", "Databricks AI Search"),
    "search.vector": ("Meaning and keyword search over the chatbot's live, unexpired documents.",
                      "Databricks AI Search"),
    "search.rerank": ("Re-reads the top results and reorders them so the best section comes first.",
                      "AI Search reranker"),
    "guardrail.retrieved_injection": ("Removes hidden instructions planted inside documents before the AI sees "
                                      "them.", "Chatbot Factory guardrails"),
    "prompt.build": ("Builds what the AI is sent: the production instructions, platform rules, your topics, the "
                     "sources and the question.", "MLflow Prompt Registry"),
    "answer.attempt_1": ("Asks the AI model for an answer with citations, streamed so time to first token is "
                         "measured. Tokens and cost are recorded here.", "Unity Gateway"),
    "answer.retake_2": ("Only when a check fails: asks the AI to fix the specific problems.", "Unity Gateway"),
    "guardrail.output_check": ("Checks every citation and quote really appears in the sources, nothing restricted "
                               "leaked, and quotes are short enough.", "Chatbot Factory guardrails"),
    "guardrail.gateway": ("A Unity Gateway safety policy blocked the request or answer.",
                          "Unity Gateway service policies"),
    "guardrail.redact": ("Masks personal numbers (SSNs, cards, account numbers) in the final answer.",
                         "Chatbot Factory guardrails"),
    "response.render": ("Formats the answer and its citations for the chat page or API.", "Model Serving"),
}
LLM_HINT = ("The raw call to the AI model, logged automatically with token counts.", "MLflow autologging")


def describe(name: str, span_type: str = "") -> tuple[str, str]:
    return SPANS.get(name) or (LLM_HINT if span_type == "LLM" else ("A step in the answer.", "MLflow Tracing"))


def _ms(span: dict) -> float:
    return max(0.0, (span["end_ns"] - span["start_ns"]) / 1e6)


def _depths(spans: list[dict]) -> dict[str, int]:
    parent = {s["span_id"]: s.get("parent_id") for s in spans}
    out = {}
    for sid in parent:
        d, p = 0, parent[sid]
        while p in parent and d < 20:
            d, p = d + 1, parent[p]
        out[sid] = d
    return out


def tree(spans: list[dict]) -> list[dict]:
    """One trace in display order (parent before children, siblings by start time)."""
    kids = defaultdict(list)
    ids = {s["span_id"] for s in spans}
    for s in spans:
        kids[s.get("parent_id") if s.get("parent_id") in ids else None].append(s)
    out = []

    def walk(pid, depth):
        for s in sorted(kids[pid], key=lambda x: x["start_ns"]):
            out.append({**s, "depth": depth, "ms": _ms(s)})
            walk(s["span_id"], depth + 1)
    walk(None, 0)
    return out


def _pct(xs: list[float], q: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))] if xs else 0.0


def aggregate(traces: list[list[dict]]) -> list[dict]:
    """Typical flow across traces: one row per step name, in typical order."""
    stats = defaultdict(lambda: {"ms": [], "offset": [], "depth": [], "cost": 0.0, "type": ""})
    for spans in traces:
        if not spans:
            continue
        t0 = min(s["start_ns"] for s in spans)
        depth = _depths(spans)
        for s in spans:
            st = stats[s["name"]]
            st["ms"].append(_ms(s))
            st["offset"].append((s["start_ns"] - t0) / 1e6 + depth[s["span_id"]] * 1e-3)
            st["depth"].append(depth[s["span_id"]])
            st["type"] = s.get("type") or st["type"]
            st["cost"] += float((s.get("attributes") or {}).get("llm.cost_usd") or 0)
    n = max(1, len([t for t in traces if t]))
    rows = [{"name": k, "type": v["type"], "depth": round(median(v["depth"])), "p50_ms": _pct(v["ms"], .5),
             "p95_ms": _pct(v["ms"], .95), "runs": len(v["ms"]) / n, "cost_per_question": v["cost"] / n,
             "durations": v["ms"], "order": median(v["offset"])}
            for k, v in stats.items()]
    return sorted(rows, key=lambda r: (r["order"], r["depth"]))


def code_excerpt(source: str, name: str, lines: int = 14) -> tuple[int, str] | None:
    """The code that opens a step's span in agent/agent.py: (line number, excerpt)."""
    src = source.splitlines()
    pat = re.compile(rf'name="{re.escape(name)}"' + (r"|name=name" if name.startswith(("answer.", "search."))
                                                    else ""))
    for i, line in enumerate(src):
        if pat.search(line):
            return i + 1, "\n".join(src[i:i + lines])
    return None
