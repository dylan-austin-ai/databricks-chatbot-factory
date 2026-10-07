"""Retrieval strategy comparison per bot (RET-3).

Runs the bot's golden questions against each strategy (directly on AI Search, no LLM
calls, so it's cheap), scores Hit@1/3/5 and MRR, applies the best and explains the
choice in one plain-English sentence. Owners don't need to know what any of it means.
"""
from __future__ import annotations

from .evals import RETRIEVAL_KS, retrieval_metrics

TIE_MARGIN = 0.01  # prefer the simpler strategy when scores are this close


def score(metrics: dict[str, float]) -> float:
    return metrics.get("mrr", 0) + 0.5 * metrics.get("hit_at_1", 0) + 0.25 * metrics.get("hit_at_5", 0)


def complexity(strategy: dict) -> int:
    return int(bool(strategy.get("rerank"))) + int(strategy.get("query_type") == "hybrid") + \
        int(strategy.get("num_results", 8) > 8)


def average(per_question: list[dict[str, float]]) -> dict[str, float]:
    keys = {k for m in per_question for k in m}
    return {k: sum(m.get(k, 0.0) for m in per_question) / len(per_question) for k in keys} \
        if per_question else {}


def evaluate(strategy: dict, cases: list[dict], search) -> dict[str, float]:
    """search(question, strategy) -> ranked [{rank, doc_id, pages}]."""
    per_q = []
    for c in cases:
        if not c.get("source_doc_id"):
            continue
        per_q.append(retrieval_metrics(search(c["question"], strategy), c["source_doc_id"],
                                       c.get("expected_pages")))
    return average(per_q)


def choose(results: list[tuple[dict, dict[str, float]]]) -> tuple[dict, dict[str, float]]:
    best_score = max(score(m) for _, m in results)
    close = [(s, m) for s, m in results if score(m) >= best_score - TIE_MARGIN]
    return min(close, key=lambda sm: complexity(sm[0]))


def explain(chosen: dict, metrics: dict[str, float], results: list[tuple[dict, dict]]) -> str:
    baseline = next((m for s, m in results if s.get("query_type") == "ann" and not s.get("rerank")), None)
    found_first = metrics.get("hit_at_1", 0)
    text = (f"We tested {len(results)} ways of searching your documents. \"{chosen.get('name')}\" "
            f"worked best: it put the right document first for {found_first:.0%} of your test questions"
            f" and in the top {RETRIEVAL_KS[-1]} for {metrics.get('hit_at_5', 0):.0%}.")
    if baseline and baseline is not metrics:
        gain = found_first - baseline.get("hit_at_1", 0)
        if gain > 0.005:
            text += f" That's {gain:.0%} better than the simplest option."
    return text
