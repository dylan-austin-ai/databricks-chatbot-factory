"""Trace flow, topic clustering and the (i) explainers."""
import pathlib

import numpy as np

from factory import flow
from factory.drift import kmeans, topic_count
from factory.explain import EXPLAIN, help_text


def _span(name, sid, parent, start, end, typ="CHAIN", cost=0):
    return {"name": name, "type": typ, "span_id": sid, "parent_id": parent, "start_ns": start * 1_000_000,
            "end_ns": end * 1_000_000, "attributes": {"llm.cost_usd": cost} if cost else {}}


def _trace(answer_ms, retake=False):
    spans = [_span("chatbot.answer", "a", None, 0, 3000, "AGENT"), _span("retrieve", "r", "a", 10, 450, "RETRIEVER"),
             _span("search.rerank", "rr", "r", 20, 440, "RERANKER"),
             _span("answer.attempt_1", "l", "a", 460, 460 + answer_ms, cost=0.002)]
    if retake:
        spans.append(_span("answer.retake_2", "l2", "a", 2600, 2900, cost=0.003))
    return spans


def test_aggregate_orders_steps_and_counts_runs_and_cost():
    rows = flow.aggregate([_trace(2000), _trace(2200, retake=True), _trace(1800)])
    names = [r["name"] for r in rows]
    assert names == ["chatbot.answer", "retrieve", "search.rerank", "answer.attempt_1", "answer.retake_2"]
    by = {r["name"]: r for r in rows}
    assert by["search.rerank"]["depth"] == 2 and by["retrieve"]["depth"] == 1
    assert by["answer.retake_2"]["runs"] == 1 / 3
    assert abs(by["answer.attempt_1"]["cost_per_question"] - 0.002) < 1e-9
    assert by["answer.attempt_1"]["p50_ms"] == 2000 and by["answer.attempt_1"]["p95_ms"] == 2200


def test_tree_puts_children_under_parents_in_time_order():
    out = flow.tree(list(reversed(_trace(2000))))
    assert [(s["name"], s["depth"]) for s in out] == [("chatbot.answer", 0), ("retrieve", 1), ("search.rerank", 2),
                                                      ("answer.attempt_1", 1)]


def test_every_agent_span_has_plain_english_and_code():
    src = (pathlib.Path(__file__).resolve().parents[1] / "agent" / "agent.py").read_text()
    for name in ["config.load", "access.check", "retrieve", "prompt.build", "guardrail.output_check", "response.render"]:
        assert name in flow.SPANS
        assert flow.code_excerpt(src, name), name
    assert flow.code_excerpt(src, "answer.attempt_1")  # opened via name=name in _chat
    assert flow.describe("ChatCompletions", "LLM") == flow.LLM_HINT


def test_kmeans_finds_separated_topics():
    rng = np.random.default_rng(1)
    a, b = rng.normal([5, 0, 0], 0.1, (40, 3)), rng.normal([0, 5, 0], 0.1, (40, 3))
    labels, cents = kmeans(np.vstack([a, b]), 2)
    assert len(set(labels[:40])) == 1 and len(set(labels[40:])) == 1 and labels[0] != labels[40]
    assert topic_count(50) == 2 and topic_count(10_000) == 12


def test_explainers_are_plain_and_linked():
    for key, (body, feature, url) in EXPLAIN.items():
        assert body and len(body) < 400, key
        assert not url or url.startswith("https://"), key
    text = help_text("prompt_optimizer")
    assert "GEPA" in text and "optimize-prompts" in text


def test_budget_alerts_go_to_mlops_only(settings):
    from factory.alerting import alert_query
    assert "kind <> 'budget'" in alert_query(settings, "claims_bot", None, 20)
    assert "kind = 'budget'" in alert_query(settings, None, None, 20)


def test_everyone_is_the_built_in_account_group():
    from factory.config import EVERYONE
    assert EVERYONE == "account users"
