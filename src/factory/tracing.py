"""Pure helpers for MLflow tracing (OBS-6..12). No mlflow import, so they unit-test anywhere.

Span tree for one question (names are stable so traces can be searched and charted):

  chatbot.answer (AGENT)                       trace tags: bot, release, outcome, cost, ttft, tokens...
  ├─ config.load                               runtime JSON from the volume
  ├─ identity.resolve / access.check (GUARDRAIL)
  ├─ history.select (MEMORY)
  ├─ query.rewrite (CHAIN) ─ llm call          only when the bot enables RET-2
  ├─ retrieve (RETRIEVER)                      final documents, rendered by the MLflow UI
  │  ├─ search.vector (RETRIEVER)              before rerank
  │  └─ search.rerank (RERANKER)               after rerank, with rank movement
  ├─ guardrail.retrieved_injection (GUARDRAIL)
  ├─ prompt.build (PARSER)
  ├─ answer.attempt_1 (CHAIN) ─ llm call (autologged CHAT_MODEL span with native token usage)
  ├─ guardrail.output_check (GUARDRAIL)        quotes, excerpts, links, leaks, citations
  ├─ answer.retake_2 ...                        only when the check fails
  ├─ guardrail.redact (GUARDRAIL)
  └─ response.render (PARSER)
"""
from __future__ import annotations

import json

from .sensitive import redact


def llm_cost(input_tokens: int, output_tokens: int, pricing: dict) -> float:
    return round(input_tokens / 1e6 * float(pricing.get("answer_input_per_mtok", 0))
                 + output_tokens / 1e6 * float(pricing.get("answer_output_per_mtok", 0)), 6)


def pages(hit: dict) -> str:
    raw = hit.get("page_ids") or []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raw = []
    return ", ".join(str(int(p) + 1) for p in raw) or "n/a"


def documents(hits: list[dict]) -> list[dict]:
    """RETRIEVER span outputs in MLflow's Document shape, so the trace UI lists the chunks and
    built-in retrieval scorers (groundedness, relevance) can read them."""
    return [{"id": h.get("chunk_id"), "page_content": h.get("chunk_to_retrieve") or "",
             "metadata": {"doc_uri": h.get("source_uri"), "doc_name": h.get("doc_name"),
                          "doc_version": h.get("doc_version"), "pages": pages(h), "rank": h.get("rank"),
                          "pre_rerank_rank": h.get("pre_rerank_rank"), "score": h.get("score")}}
            for h in hits]


def rerank_movement(hits: list[dict]) -> dict:
    """How much the reranker changed the order: how many results moved, and the largest jump."""
    moves = [abs((h.get("pre_rerank_rank") or 0) - h["rank"]) for h in hits if h.get("pre_rerank_rank")]
    new = sum(1 for h in hits if not h.get("pre_rerank_rank"))
    return {"moved": sum(1 for m in moves if m), "max_jump": max(moves or [0]), "new_from_rerank": new}


def trace_tags(log: dict) -> dict[str, str]:
    """Searchable trace tags (MLflow tag values are strings)."""
    keys = ["bot_id", "channel", "release_id", "config_version", "outcome", "model", "platform_version",
            "environment", "identity_mode", "synthetic", "critical", "latency_ms", "ttft_ms", "cost_usd",
            "llm_calls", "retakes", "top_score", "prompt_uri"]
    tags = {k: (str(log[k]).lower() if isinstance(log[k], bool) else str(log[k]))
            for k in keys if log.get(k) is not None}
    tags["input_tokens"], tags["output_tokens"] = str(log["tokens"][0]), str(log["tokens"][1])
    tags["guardrails_fired"] = ",".join(sorted(set(log.get("fired", []))))
    tags["client_request_id"] = log["request_id"]
    for name, on in (log.get("judges") or {}).items():  # per-bot production judge switches (JDG-3)
        tags[f"judge_{name}"] = "on" if on else "off"
    return tags


# Production judges, each switchable per bot (on unless noted). Multi-turn judges score a whole
# conversation (session) and are off by default.
PRODUCTION_JUDGES = ("groundedness", "retrieval_relevance", "retrieval_sufficiency", "relevance", "safety",
                     "platform_rules", "multi_turn")


def judge_filters(critical: bool, judge: str) -> str:
    """Production-scorer trace filter: live, real traffic where the bot has this judge on, split by
    critical flag so critical bots are judged at 100% and the rest at the admin rate (MON-6)."""
    return (f"tags.channel = 'live' AND tags.synthetic = 'false' AND tags.judge_{judge} = 'on' "
            f"AND tags.critical = '{'true' if critical else 'false'}'")


def assessment_value(value) -> float | None:
    """Normalize a judge result (bool, yes/no, pass/fail, number) to 1.0/0.0 or a number."""
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().lower()
    if text in {"yes", "pass", "true", "correct"}:
        return 1.0
    if text in {"no", "fail", "false", "incorrect"}:
        return 0.0
    return None


def mask(value):
    """Recursively redact restricted number patterns in span inputs/outputs (OBS-14)."""
    if isinstance(value, str):
        return redact(value)[0]
    if isinstance(value, dict):
        return {k: mask(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [mask(v) for v in value]
    return value
