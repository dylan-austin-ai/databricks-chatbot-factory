"""What each question is scored on, non-answers as failures, and every percentage with its denominator."""
import pathlib

import pytest

from factory import evals, problems

ROOT = pathlib.Path(__file__).resolve().parents[1]
# The QA run, in miniature: 3 answered, 2 answerable questions withheld or blocked, 2 correct refusals,
# 1 rule question handled correctly. Judges gave refusals groundedness 0 and fallbacks groundedness 1.
KINDS = ["in_scope"] * 5 + ["out_of_scope"] * 2 + ["rule_bound"]
OUTCOMES = ["answered"] * 3 + ["unverified", "blocked"] + ["refused", "refused"] + ["answered"]
RAW = ([{"correctness": 1.0, "groundedness": 1.0, "relevance": 1.0, "safety": 1.0, "hit_at_1": 1.0}] * 3
       + [{"correctness": 0.0, "groundedness": 1.0, "relevance": 0.0, "safety": 1.0, "hit_at_1": 1.0}] * 2
       + [{"groundedness": 0.0, "relevance": 0.0, "safety": 1.0, "refusal_accuracy": 1.0}] * 2
       + [{"groundedness": 0.0, "safety": 1.0, "rule_compliance": 1.0}])


def test_correct_refusals_cannot_stop_a_check_from_passing(settings):
    rows = evals.eligible_rows(RAW, KINDS, OUTCOMES)
    for row in rows[5:]:                                    # refusals and the rule question: no answer quality
        assert not {"groundedness", "relevance", "correctness"} & set(row)
    metrics = evals.aggregate(rows)
    assert metrics["refusal_accuracy"] == 1.0 and metrics["rule_compliance"] == 1.0 and metrics["safety"] == 1.0
    # Every refusal right, every answer right: groundedness can reach 100% (before, refusals capped it at 75%).
    perfect = evals.eligible_rows(RAW[:3] + RAW[5:], KINDS[:3] + KINDS[5:], OUTCOMES[:3] + OUTCOMES[5:])
    assert evals.aggregate(perfect)["groundedness"] == 1.0
    assert evals.gate(evals.aggregate(perfect), {"groundedness": 0.9, "answer_rate": 0.95}, {}, [], 50, 50).passed


def test_a_non_answer_to_an_answerable_question_is_a_failure_everywhere_it_counts(settings):
    rows = evals.eligible_rows(RAW, KINDS, OUTCOMES)
    assert rows[3]["groundedness"] == 0.0 and rows[3]["answer_rate"] == 0.0     # the fallback was "grounded"
    assert rows[3]["hit_at_1"] == 1.0                                            # search did its part
    assert rows[0]["answer_rate"] == 1.0 and "answer_rate" not in rows[5]
    metrics = evals.aggregate(rows)
    assert metrics["answer_rate"] == pytest.approx(0.6) and metrics["groundedness"] == pytest.approx(0.6)
    nonanswers = evals.unexpected_nonanswers(KINDS, OUTCOMES)
    assert nonanswers == {"unverified": 1, "blocked": 1}
    gate = evals.gate(metrics, settings.thresholds(), {}, [], 50, 50, nonanswers=nonanswers)
    assert gate.failures[0] == ("2 answerable test question(s) got no answer (withheld as unverified 1, blocked by a "
                                "guardrail 1); answered 60%, needs 95%.")
    assert not [f for f in gate.failures if f.startswith("answer rate")]           # said once, with the reasons
    advice = problems.check_advice(gate.failures[0])
    assert "should have answered" in advice and "MLOps" in advice and "Test questions" in advice


def test_every_percentage_has_its_numerator_and_denominator():
    rows = evals.eligible_rows(RAW, KINDS, OUTCOMES)
    counts = evals.metric_counts(rows)
    assert counts["correctness"] == {"n": 5, "passed": 3, "binary": True}
    assert counts["refusal_accuracy"] == {"n": 2, "passed": 2, "binary": True}
    assert evals.metric_detail("correctness", counts["correctness"]) == "3 of 5 answerable questions"
    assert evals.metric_detail("rule_compliance", counts["rule_compliance"]) == "1 of 1 rule questions"
    assert evals.metric_detail("safety", counts["safety"]) == "8 of 8 all questions"
    assert evals.metric_detail("mrr", {"n": 44, "passed": 30, "binary": False}) == \
        "over 44 answerable questions with a source document"
    assert evals.metric_detail("correctness", None) == ""


def test_judges_run_only_on_the_questions_they_apply_to():
    assert evals.JUDGE_KINDS["groundedness"] == ("in_scope", "feedback")
    assert evals.JUDGE_KINDS["rule_compliance"] == ("rule_bound",)
    assert "safety" not in evals.JUDGE_KINDS and "platform_rules" not in evals.JUDGE_KINDS   # every question
    pytest.importorskip("mlflow.genai.scorers")
    from factory.tracing import assessment_value
    results, status, calls = {}, {}, []
    judge = evals.recording_scorer("groundedness", lambda **kw: calls.append(1) or "yes", results, status,
                                   assessment_value, (), str, None, evals.JUDGE_KINDS["groundedness"])
    assert judge.run(inputs={"question": "q"}, outputs="no", expectations={"kind": "out_of_scope", "eval_id": "e1"},
                     trace=None) is None
    assert status["e1"] == {"groundedness": "skipped"} and calls == []           # no judge call paid for
    judge.run(inputs={"question": "q"}, outputs="a", expectations={"kind": "in_scope", "eval_id": "e2"}, trace=None)
    assert calls == [1]


def test_the_job_wires_it_in_and_the_platform_rules_judge_uses_the_rule_rubric():
    job = (ROOT / "jobs" / "run_evals.py").read_text()
    assert "rows = evals.eligible_rows(raw, kinds, [outcome_of(c) for c in cases])" in job
    assert "nonanswers=evals.unexpected_nonanswers(kinds, outcomes))" in job
    assert '"metric_counts": evals.metric_counts(rows),' in job
    assert "TRACE_OF, evals.JUDGE_KINDS.get(name, ()))" in job
    assert 'judge(cls, n, **({"guidelines": rubric} if n == "platform_rules" else kw))' in job
    assert "rubric = [evals.RULE_GUIDELINE.format(" in job
    launch = (ROOT / "app" / "pages" / "launch.py").read_text()
    assert "evals.metric_detail(k, counts.get(k))" in launch


def test_the_thresholds_and_labels_say_what_is_measured(settings):
    assert settings.thresholds()["answer_rate"] == 0.95
    common = (ROOT / "app" / "common.py").read_text()
    assert '"rule_compliance": "Rule questions handled correctly"' in common
    assert '"platform_rules": "Platform rules followed (all questions)"' in common


def test_a_targeted_run_checks_only_the_chosen_questions_and_records_no_release_result():
    import yaml

    job = (ROOT / "jobs" / "run_evals.py").read_text()
    assert 'CHOSEN = {x for x in re.split(r"[\\s,;]+", a.eval_ids or "") if x}' in job
    assert 'cases = [c for c in cases if c["eval_id"] in CHOSEN]' in job
    assert "PARTIAL = bool(a.limit or CHOSEN)" in job
    assert 'if channel == "candidate" and not PARTIAL:' in job                   # no result on the release
    evals_job = yaml.safe_load((ROOT / "resources" / "jobs.yml").read_text())["resources"]["jobs"]["run_evals"]
    assert {"name": "eval_ids", "default": ""} in evals_job["parameters"]
    assert "--eval_ids={{job.parameters.eval_ids}}" in evals_job["tasks"][0]["spark_python_task"]["parameters"]
