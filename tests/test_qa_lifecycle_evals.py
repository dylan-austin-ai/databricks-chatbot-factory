import json

import pytest

from factory import evals, qa
from factory.lifecycle import TransitionError, check_transition


@pytest.fixture
def th(settings):
    return settings.get("quality.readability")


def test_text_files_are_na_not_low(th):
    r = qa.classify({"is_text": True}, th)
    assert r.badge == "n/a"


def test_clean_doc_is_green(th):
    r = qa.classify({"n_pages": 2, "text_pages": [0, 1], "total_chars": 4000,
                     "mean_confidence": 0.95, "errors_json": "[]"}, th)
    assert r.badge == "green"


def test_parse_errors_reported_per_page(th):
    r = qa.classify({"n_pages": 5, "text_pages": [0, 1, 2, 4], "total_chars": 9000,
                     "mean_confidence": 0.9,
                     "errors_json": json.dumps([{"page_id": 3, "error_message": "x"}])}, th)
    assert r.badge == "red" and "We couldn't read page 4." in r.messages


def test_empty_pages_and_low_confidence(th):
    r = qa.classify({"n_pages": 4, "text_pages": [0, 1, 2], "total_chars": 4000,
                     "mean_confidence": 0.7, "errors_json": "[]"}, th)
    assert r.badge == "yellow"
    assert any("page(s) 4" in m for m in r.messages)


def test_visual_findings_escalate(th):
    r = qa.classify({"n_pages": 1, "text_pages": [0], "total_chars": 900,
                     "mean_confidence": 0.95, "errors_json": "[]"}, th)
    r = qa.merge_visual(r, [{"page": 1, "severity": "major", "issue": "Table scrambled"}])
    assert r.badge == "red"


def test_prompts_format_cleanly():
    qa.VISUAL_JUDGE_PROMPT.format(page=1, text="x")
    qa.GOLDEN_SET_PROMPT.format(purpose="p", n_easy=3, n_hard=2, doc_name="d", text="t")
    qa.OUT_OF_SCOPE_PROMPT.format(purpose="p", refuse="r", n=3)


def test_transitions():
    check_transition("pending_approval", "live")
    check_transition("live", "budget_paused")
    check_transition("budget_paused", "live")
    with pytest.raises(TransitionError):
        check_transition("draft", "live")
    with pytest.raises(TransitionError):
        check_transition("testing", "live")       # must go through approval
    with pytest.raises(TransitionError):
        check_transition("deleted", "testing")


def test_combined_gate(settings):
    th = settings.thresholds()
    zero = settings.get("quality.zero_failure_checks")
    good = {k: 1.0 for k in th}
    assert evals.gate(good, th, {}, zero, 10, 5).passed
    r = evals.gate({**good, "hit_at_1": 0.7}, th, {}, zero, 10, 5)
    assert not r.passed and "hit at 1 is 70%" in r.failures[0]
    assert not evals.gate(good, th, {"leakage": 1}, zero, 10, 5).passed       # zero tolerance
    assert not evals.gate(good, th, {}, zero, 3, 5).passed                    # too few questions
    unsafe = {**good, "safety": 0.9}
    r = evals.gate(unsafe, th, {}, zero, 10, 5)
    assert not r.passed and r.needs_safety_review
    assert evals.gate(unsafe, th, {}, zero, 10, 5, safety_override=True).passed


def test_retrieval_metrics():
    ranked = [{"rank": 1, "doc_id": "a", "pages": "1"}, {"rank": 2, "doc_id": "b", "pages": "4, 5"}]
    m = evals.retrieval_metrics(ranked, "b", [5])
    assert m["hit_at_1"] == 0 and m["hit_at_3"] == 1 and m["mrr"] == 0.5 and m["page_hit_at_3"] == 1
    assert evals.retrieval_metrics(ranked, "zzz")["mrr"] == 0


def test_aggregate_skips_missing_scores():
    assert evals.aggregate([{"correctness": 1.0}, {"correctness": 0.0}, {"correctness": None}]) == {"correctness": 0.5}


def test_sampling_judges_all_critical():
    rows = [{"bot_id": "crit"} for _ in range(100)] + [{"bot_id": "norm"} for _ in range(1000)]
    picked = evals.sample_for_judging(rows, {"crit"}, 0.2, seed=7)
    assert sum(r["bot_id"] == "crit" for r in picked) == 100
    assert 150 < sum(r["bot_id"] == "norm" for r in picked) < 250


def test_citation_accuracy():
    src = ["Claims over $10,000 require supervisor approval."]
    assert evals.citation_accuracy('Per page 2, "Claims over $10,000 require supervisor approval" [1].', src)[0]
    assert not evals.citation_accuracy("Claims need approval.", src)[0]
    assert not evals.citation_accuracy('"Claims under $10 are free" [1]', src)[0]
