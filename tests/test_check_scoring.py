"""Who the chatbot treats as the platform's own caller, and how a quality check keeps its scores."""
import pathlib
import threading
import time
from types import SimpleNamespace

import pytest

from factory import evals
from factory.config import PlatformSettings
from factory.identity import is_platform_caller, platform_callers
from factory.problems import safe_text
from factory.tracing import assessment_value

ROOT = pathlib.Path(__file__).resolve().parents[1]


def with_access(**access):
    return PlatformSettings.load({"catalog": "qa_cf", **{f"access.{k}": v for k, v in access.items()}})


# Who gets past a chatbot's access list ---------------------------------------------------------
def test_the_jobs_identity_is_one_of_the_platforms_own_callers():
    s = with_access(jobs_run_as="Admin@Corp.com", synthetic_callers=["monitor-sp"], trusted_callers=["app-sp"])
    assert platform_callers(s) == {"admin@corp.com", "monitor-sp", "app-sp"}
    for caller in ("admin@corp.com", "ADMIN@corp.com ", "monitor-sp", "app-sp"):
        assert is_platform_caller(caller, s), caller


def test_nobody_else_is_and_an_empty_setting_matches_nobody():
    s = with_access(jobs_run_as="admin@corp.com")
    for caller in ("someone@corp.com", "admin@corp.com.evil.example", "", None, "   "):
        assert not is_platform_caller(caller, s), caller
    unset = with_access()                                       # the defaults: nothing named anywhere
    assert platform_callers(unset) == set()
    for caller in ("", None, "admin@corp.com"):
        assert not is_platform_caller(caller, unset), caller
    assert platform_callers(with_access(jobs_run_as="  ", synthetic_callers=["", None])) == set()


def test_the_agent_honours_the_test_flag_only_from_those_callers():
    source = (ROOT / "agent" / "agent.py").read_text()
    # both halves are needed: the request says it is a test question AND its caller is the platform's own
    assert 'synthetic = bool(ci.get("synthetic")) and idn.is_platform_caller(caller, s)' in source
    assert "allowed = synthetic or self.authorize(cfg, who, obo, channel, s)" in source   # everyone else: as usual
    assert source.count("is_platform_caller(") == 2             # access, and who may be told why it failed
    assert 'access.trusted_callers", []) + s.get("access.synthetic_callers"' not in source   # no second rule


def test_the_check_and_its_diagnostic_use_that_same_rule_to_explain_a_refusal():
    job = (ROOT / "jobs" / "run_evals.py").read_text()
    assert "from factory.identity import is_platform_caller" in job
    assert "evals.chatbot_fault(custom, RUNS_AS, is_platform_caller(RUNS_AS, settings))" in job
    assert "DIAG: 0. this run calls the chatbot as" in job and "THE SETTINGS DO NOT NAME THAT IDENTITY" in job


# What a check can't score ----------------------------------------------------------------------
def test_a_refused_question_is_a_fault_that_names_the_identity_and_the_setting():
    not_named = evals.chatbot_fault({"outcome": "denied"}, "admin@corp.com", recognised=False)
    assert "refused the check's own question" in not_named and "admin@corp.com" in not_named
    assert "set access.jobs_run_as to it" in not_named and "deploy_agent" not in not_named
    named = evals.chatbot_fault({"outcome": "denied"}, "admin@corp.com", recognised=True)
    assert "already name that identity" in named and "run the deploy_agent job" in named
    assert "could not be read" in evals.chatbot_fault({"outcome": "denied"})


def test_a_check_stopped_by_a_refusal_is_shown_as_a_setup_fault_not_as_try_again():
    from factory import problems

    stopped = ("The chatbot gave a test question no answer, so there is nothing to score for hr_chatbot: "
               + evals.chatbot_fault({"outcome": "denied"}, "admin@corp.com"))
    kind, advice = problems.job_stopped(stopped)
    assert kind == problems.SETUP and "starting it again won't help" in advice


def test_an_error_and_an_unavailable_chatbot_are_faults_and_an_answer_is_not():
    assert "PermissionDenied: no" in evals.chatbot_fault({"outcome": "error", "error": "PermissionDenied: no"})
    assert "no reason was given" in evals.chatbot_fault({"outcome": "error"})
    assert "not available" in evals.chatbot_fault({"outcome": "unavailable"})
    for outcome in ("answered", "refused", "no_source", "blocked", "unverified", None):
        assert evals.chatbot_fault({"outcome": outcome}) == "", outcome
    assert evals.chatbot_fault(None) == ""


# Scores, kept as the scorers return them ------------------------------------------------------
def fb(name, value=None, rationale=None, error=None):
    return SimpleNamespace(name=name, value=value, rationale=rationale,
                           error=SimpleNamespace(error_message=error, error_code="E") if error else None)


def test_what_a_scorer_returns_becomes_a_score_or_a_reason_never_a_silent_zero():
    kept = evals.feedback_results("correctness", fb("correctness", "yes", "matches the expected answer"), assessment_value)
    assert kept == {"correctness": {"value": 1.0, "rationale": "matches the expected answer", "error": ""}}
    assert evals.feedback_results("safety", True, assessment_value)["safety"]["value"] == 1.0   # a plain value
    assert evals.feedback_results("x", fb("feedback", "no"), assessment_value)["x"]["value"] == 0.0   # unnamed: the scorer's
    assert evals.feedback_results("x", None, assessment_value) == {}                           # it chose not to score

    failed = evals.feedback_results("groundedness", fb("groundedness", error="429 slow down\n  stack"), assessment_value,
                                    safe_text)["groundedness"]
    assert failed == {"value": None, "rationale": "", "error": "429 slow down stack"}
    odd = evals.feedback_results("relevance", fb("relevance", "maybe"), assessment_value)["relevance"]
    assert odd["value"] is None and "'maybe', which is not a verdict" in odd["error"]


def test_several_results_under_one_name_are_averaged_and_code_checks_keep_their_own_names():
    chunks = [fb("retrieval_relevance/precision", 0.5), fb("retrieval_relevance", "yes"), fb("retrieval_relevance", "no")]
    kept = evals.feedback_results("retrieval_relevance", chunks, assessment_value)
    assert kept["retrieval_relevance"]["value"] == 0.5 and kept["retrieval_relevance/precision"]["value"] == 0.5
    checks = evals.feedback_results("checks", [fb("hit_at_1", 1.0), fb("citation_accuracy", False)], assessment_value)
    assert {k: v["value"] for k, v in checks.items()} == {"hit_at_1": 1.0, "citation_accuracy": 0.0}


def test_a_questions_results_split_into_scores_rationales_and_errors():
    results = {"correctness": {"value": 1.0, "rationale": "fine", "error": ""},
               "groundedness": {"value": None, "rationale": "", "error": "429 slow down"},
               "checks": {"value": None, "rationale": "", "error": ""}}
    scores, why, errors = evals.scores_from(results)
    assert scores == {"correctness": 1.0} and why == {"correctness": "fine"}
    assert errors == {"groundedness": "429 slow down", "checks": "it gave no result"}
    assert evals.scores_from(None) == ({}, {}, {})               # a question no scorer reported on: unscored
    assert evals.scorer_problems([errors, {"groundedness": "timeout"}, {}]) == {
        "checks": "it gave no result (on 1 of 3 question(s))", "groundedness": "429 slow down (on 2 of 3 question(s))"}


def test_the_gate_says_why_a_required_judge_gave_no_score(settings):
    thresholds = settings.thresholds()
    scored = {name: 1.0 for name in thresholds if name != "groundedness"}
    said = evals.gate(scored, thresholds, {}, [], 60, 50, required=["groundedness", "correctness"],
                      scorer_errors={"groundedness": "429 slow down (on 50 of 50 question(s))"})
    assert said.failures == ["groundedness wasn't scored: 429 slow down (on 50 of 50 question(s)); re-run the check."]
    plain = evals.gate(scored, thresholds, {}, [], 60, 50, required=["groundedness"])
    assert plain.failures == ["groundedness wasn't scored (judge unavailable?); re-run the check."]


def test_secrets_never_ride_along_in_a_scorers_error():
    assert safe_text("call failed: Bearer abc.def.ghi\n token=xyz") == "call failed: [hidden] [hidden]"
    assert len(safe_text("x" * 900)) == 300


# The scorer that does the keeping, on real MLflow scorer objects ------------------------------------
@pytest.fixture
def kept():
    pytest.importorskip("mlflow.genai.scorers")
    results, status = {}, {}

    def make(name, work, needs=""):
        return evals.recording_scorer(name, work, results, status, assessment_value, needs, safe_text)
    return SimpleNamespace(results=results, status=status, make=make)


ROW = {"inputs": {"question": "How do I file a claim?"}, "outputs": "Like this.", "expectations": {"kind": "in_scope"},
       "trace": None}
Q = ROW["inputs"]["question"]


def test_it_hands_back_exactly_what_the_judge_returned_and_keeps_a_copy(kept):
    from mlflow.entities import Feedback

    verdict = Feedback(name="correctness", value="yes", rationale="matches")
    seen = {}
    scorer = kept.make("correctness", lambda **row: seen.update(row) or verdict)
    assert scorer.name == "correctness" and scorer.run(**ROW) is verdict
    assert set(seen) == {"inputs", "outputs", "expectations", "trace"}        # the judge gets the whole row
    assert kept.results[Q]["correctness"] == {"value": 1.0, "rationale": "matches", "error": ""}
    assert kept.status[Q] == {"correctness": "done"}


def test_a_judge_that_needs_guidelines_is_skipped_on_a_question_without_any(kept):
    def needs_guidelines(*, expectations=None, **_):
        if not (expectations or {}).get("guidelines"):
            raise RuntimeError("Guidelines must be specified")       # what MLflow's own judge does
        return "yes"

    scorer = kept.make("expectations_guidelines", needs_guidelines, needs="guidelines")
    assert scorer.run(**ROW) is None                                 # no failure, no score, nothing attached
    assert kept.status[Q] == {"expectations_guidelines": "skipped"} and Q not in kept.results
    refuse = {**ROW, "inputs": {"question": "Is my claim covered?"},
              "expectations": evals.dataset_record({"kind": "out_of_scope", "eval_id": "e2",
                                                    "question": "Is my claim covered?"})["expectations"]}
    assert scorer.run(**refuse) == "yes"
    assert kept.results["Is my claim covered?"]["expectations_guidelines"]["value"] == 1.0
    job = (ROOT / "jobs" / "run_evals.py").read_text()
    assert 'NEEDS = {"expectations_guidelines": "guidelines"}' in job


def test_a_judge_that_raises_still_raises_and_its_reason_is_kept(kept):
    def broken(**_):
        raise ValueError("judge endpoint said no\n  File ..., line 1")

    scorer = kept.make("groundedness", broken)
    with pytest.raises(ValueError):                                  # MLflow records it on the trace as before
        scorer.run(**ROW)
    assert kept.results[Q]["groundedness"] == {"value": None, "rationale": "", "error": "ValueError: judge endpoint said no"}
    assert kept.status[Q]["groundedness"] == "failed"
    kept.make("groundedness", lambda **_: "yes").run(**ROW)          # MLflow tries again and it works
    assert kept.results[Q]["groundedness"]["value"] == 1.0 and kept.status[Q]["groundedness"] == "done"


def test_a_judge_that_never_comes_back_is_reported_as_unfinished(kept):
    release = threading.Event()
    scorer = kept.make("safety", lambda **_: release.wait(20) and "yes")
    thread = threading.Thread(target=lambda: scorer.run(**ROW), daemon=True)
    thread.start()
    for _ in range(100):
        if kept.status.get(Q, {}).get("safety") == "running":
            break
        time.sleep(0.02)
    evals.close_unfinished(kept.results, kept.status)
    assert kept.results[Q]["safety"] == {"value": None, "rationale": "", "error": evals.UNFINISHED}
    release.set()
    thread.join(5)


# The job decides from those, and proves the endpoint's own traces are stored -------------------------------
def test_the_check_decides_from_what_the_scorers_returned_not_from_a_read_back():
    job = (ROOT / "jobs" / "run_evals.py").read_text()
    decide = job[job.index("mlflow.genai.evaluate(data=records"):job.index("gate = evals.gate(")]
    assert 'scored = {c["question"]: evals.scores_from(RESULTS.get(c["question"])) for c in cases}' in decide
    outside_diagnostic = decide[:decide.index("if DIAGNOSE:\n            print(f\"DIAG: 9.")]
    assert "read_scores(" not in outside_diagnostic and "search_traces" not in outside_diagnostic
    assert "evals.close_unfinished(RESULTS, STATUS)" in outside_diagnostic
    assert "return [kept(n, j.run) for n, j in judges.items()] + [kept(\"checks\", checks)]" in job   # every scorer
    assert "for store in (OUTCOMES, TRACE_IDS, RESULTS, STATUS):\n        store.clear()" in job       # per chatbot
    assert '"scorer_errors": scorer_errors' in job and '"errors": scored[c["question"]][2]' in job


def test_the_diagnostic_reads_back_the_endpoints_own_trace_and_each_questions_results():
    job = (ROOT / "jobs" / "run_evals.py").read_text()
    assert "1c. the chatbot's own trace, written by the serving endpoint, is read back" in job
    assert 'agent_trace = str(said.get("trace_id") or "")' in job
    assert "evals.wait_for(lambda: mlflow.get_trace(agent_trace))" in job             # it arrives in the background
    assert "THE SERVING ENDPOINT'S OWN TRACES ARE NOT PROVEN TO BE SAVED" in job
    for line in ("NO SCORE from {name}: {why}", "this check's trace is read back by its id",
                 "the chatbot's own trace for it is in storage", "10. this run's traces are found by searching"):
        assert line in job, line


def test_waiting_for_storage_gives_up_after_its_tries_and_says_why():
    answers = iter([None, RuntimeError("NOT_FOUND: no such trace\nmore"), "the trace"])
    naps = []

    def read():
        answer = next(answers)
        if isinstance(answer, Exception):
            raise answer
        return answer

    assert evals.wait_for(read, tries=5, pause_s=10, sleep=naps.append) == ("the trace", "")
    assert naps == [10, 10]                                          # no pause after it turned up

    def never():
        raise LookupError("PERMISSION_DENIED: cannot read")
    naps.clear()
    assert evals.wait_for(never, tries=3, pause_s=1, sleep=naps.append) == (None, "LookupError: PERMISSION_DENIED: cannot read")
    assert naps == [1, 1]                                            # and none after the last try
    assert evals.wait_for(lambda: None, tries=2, pause_s=0, sleep=naps.append) == (None, "")
