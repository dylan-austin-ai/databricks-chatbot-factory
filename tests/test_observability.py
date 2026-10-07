"""Observability helpers: span payloads, trace tags, cost, scorer filters, drift math."""
from datetime import datetime, timezone

import pytest

from factory import monitoring as mon
from factory import tracing as tr


def test_llm_cost(settings):
    p = {"answer_input_per_mtok": 1.0, "answer_output_per_mtok": 5.0}
    assert tr.llm_cost(1_000_000, 200_000, p) == 2.0
    assert tr.llm_cost(0, 0, {}) == 0


def test_retriever_documents_shape():
    hits = [{"chunk_id": "c1", "chunk_to_retrieve": "text", "source_uri": "/v/a.pdf", "doc_name": "A",
             "doc_version": 2, "page_ids": "[0, 1]", "rank": 1, "pre_rerank_rank": 3, "score": 0.9}]
    d = tr.documents(hits)[0]
    assert d["id"] == "c1" and d["page_content"] == "text"
    assert d["metadata"]["doc_uri"] == "/v/a.pdf" and d["metadata"]["pages"] == "1, 2"


def test_rerank_movement():
    hits = [{"rank": 1, "pre_rerank_rank": 3}, {"rank": 2, "pre_rerank_rank": 2}, {"rank": 3, "pre_rerank_rank": None}]
    assert tr.rerank_movement(hits) == {"moved": 1, "max_jump": 2, "new_from_rerank": 1}


def test_trace_tags_are_strings_and_searchable():
    log = {"request_id": "r1", "bot_id": "b", "channel": "live", "synthetic": False, "critical": True,
           "outcome": "answered", "tokens": [100, 20], "ttft_ms": 850, "cost_usd": 0.0002,
           "fired": ["quote_check", "quote_check"], "release_id": None}
    t = tr.trace_tags(log)
    assert t["synthetic"] == "false" and t["critical"] == "true"
    assert t["input_tokens"] == "100" and t["ttft_ms"] == "850" and t["client_request_id"] == "r1"
    assert t["guardrails_fired"] == "quote_check" and "release_id" not in t
    assert all(isinstance(v, str) for v in t.values())


def test_judge_filters_split_by_critical_and_switch():
    f = tr.judge_filters(True, "safety")
    assert "tags.critical = 'true'" in f and "tags.judge_safety = 'on'" in f and "tags.synthetic = 'false'" in f
    assert "tags.critical = 'false'" in tr.judge_filters(False, "safety")
    tags = tr.trace_tags({"request_id": "r", "tokens": [0, 0], "judges": {"safety": True, "multi_turn": False}})
    assert tags["judge_safety"] == "on" and tags["judge_multi_turn"] == "off"


@pytest.mark.parametrize("value,expected", [(True, 1.0), (False, 0.0), ("yes", 1.0), ("No", 0.0),
                                            (0.7, 0.7), ("maybe", None)])
def test_assessment_value(value, expected):
    assert tr.assessment_value(value) == expected


def test_ttft_alert(settings):
    m = settings.get("monitoring")
    ts = datetime(2026, 10, 5, 16, 0, tzinfo=timezone.utc)
    row = {"bot_id": "b", "requests": 50, "errors": 0, "p50_ms": 1000, "p95_ms": 2000, "p99_ms": 3000,
           "availability": 1.0, "harmful_blocks": 0, "p95_ttft_ms": 6000}
    assert any("first token" in a.message for a in mon.evaluate_window(row, m, {}, None, ts))
    row["p95_ttft_ms"] = 900
    assert not any("first token" in a.message for a in mon.evaluate_window(row, m, {}, None, ts))


def test_pca_2d_separates_clusters():
    import numpy as np
    from factory.drift import pca_2d
    rng = np.random.default_rng(0)
    a = rng.normal(0, 0.1, (20, 16)) + 1
    b = rng.normal(0, 0.1, (20, 16)) - 1
    xy = pca_2d(np.vstack([a, b]))
    assert xy.shape == (40, 2)
    assert (xy[:20, 0].mean() > 0) != (xy[20:, 0].mean() > 0)  # the two topics land apart
