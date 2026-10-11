"""The path to launch and plain-language errors."""
import pytest

from factory import journey, problems
from factory.ingestion import BotPaths
from factory.journey import DONE, LATER, NOW, PROBLEM, WAITING

DOCS_OK = {"reading": 0, "waiting": 0, "flagged": 0, "approved": 4}
QUESTIONS_OK = {"total": 60, "approved": 55}


def _status(path):
    return [step.status for step in path]


def test_a_new_chatbot_starts_with_adding_documents():
    path = journey.steps("testing", None, None, 50, None, False, "lead@corp.com")
    assert [s.title for s in path] == ["Finish setup", "Add documents", "Review your documents",
                                       "Approve test questions", "Pass the quality check", "Get approval and publish"]
    assert _status(path) == [DONE, NOW, LATER, LATER, LATER, LATER]
    assert path[1].page == "documents" and "Add documents" in path[1].detail


def test_exactly_one_step_is_current_whatever_the_situation():
    for state in ("draft", "provisioning", "provision_failed", "testing", "pending_approval", "live"):
        for docs in (None, {"reading": 2}, {"waiting": 1, "approved": 3}, {"flagged": 1}, DOCS_OK):
            for questions in (None, {"total": 10, "approved": 3}, QUESTIONS_OK):
                for cand in (None, {}, {"eval_run_id": "e", "eval_passed": "false"},
                             {"eval_passed": "true", "review_requested_at": "2026-01-01"}):
                    path = journey.steps(state, docs, questions, 50, cand, False)
                    assert sum(s.status in (NOW, WAITING, PROBLEM) for s in path) <= 1
                    open_steps = [s.status for s in path if s.status != DONE]
                    assert all(s == LATER for s in open_steps[1:])


def test_setup_states_say_whose_move_it_is():
    assert journey.steps("provisioning", None, None, 50, None, False)[0].status == WAITING
    failed = journey.steps("provision_failed", None, None, 50, None, False)[0]
    assert failed.status == PROBLEM and "Try again" in failed.detail and failed.page == "launch"
    draft = journey.steps("draft", None, None, 50, None, False)[0]
    assert draft.status == NOW and draft.page == "create"


def test_documents_step_follows_what_the_documents_need():
    def review(docs):
        return journey.steps("testing", docs, None, 50, None, False)[2]

    assert review({"flagged": 1, "waiting": 5}).status == PROBLEM and "1 document has a problem" in review({"flagged": 1}).detail
    assert review({"waiting": 2}).status == NOW and "2 documents are waiting" in review({"waiting": 2}).detail
    assert review({"reading": 3}).status == WAITING
    done = review({"approved": 2, "reading": 1})
    assert done.status == DONE and "2 documents approved" in done.detail and "1 more still being read" in done.detail


def test_questions_step_counts_what_is_left():
    def q(questions):
        return journey.steps("testing", DOCS_OK, questions, 50, None, False)[3]

    assert q(None).status == NOW and "Suggest questions" in q(None).detail
    assert "12 of 50 approved, so 38 more to go" in q({"total": 70, "approved": 12}).detail
    assert q(QUESTIONS_OK).status == DONE


def test_quality_check_and_approval():
    def tail(cand, **kw):
        return journey.steps("testing", DOCS_OK, QUESTIONS_OK, 50, cand, False, "lead@corp.com", **kw)[4:]

    assert [s.status for s in tail(None)] == [WAITING, LATER]
    assert [s.status for s in tail({"release_id": "r1"})] == [NOW, LATER]
    assert "Run the quality check" in tail({"release_id": "r1"})[0].detail
    assert tail({"eval_run_id": "e1", "eval_passed": "false"})[0].status == PROBLEM
    passed = {"eval_run_id": "e1", "eval_passed": True, "review_requested_at": None}
    assert [s.status for s in tail(passed)] == [DONE, NOW] and "Send for approval" in tail(passed)[1].detail
    sent = {**passed, "review_requested_at": "2026-10-01"}
    assert tail(sent)[1].status == WAITING and "lead@corp.com" in tail(sent)[1].detail
    assert tail(sent, is_reviewer=True)[1].status == NOW and "Approve and publish" in tail(sent, is_reviewer=True)[1].detail


def test_next_step_for_chatbots_that_are_already_running():
    done = journey.steps("live", DOCS_OK, QUESTIONS_OK, 50, None, True)
    assert all(s.status == DONE for s in done)
    assert journey.next_step("live", done).detail.startswith("Live and answering")
    assert "Resume" in journey.next_step("paused", done).detail
    assert "budget" in journey.next_step("budget_paused", done).detail
    assert journey.next_step("archived", done).status == WAITING
    # a new version on its way: the owner is told what that version needs
    new_version = journey.steps("live", {"waiting": 1, "approved": 4}, QUESTIONS_OK, 50, {"release_id": "r2"}, True)
    assert journey.next_step("live", new_version, has_candidate=True).title == "Review your documents"
    assert journey.next_step("testing", journey.steps("testing", None, None, 50, None, False)).title == "Add documents"


def test_progress_for_many_chatbots_is_one_statement(settings):
    statement, params = journey.progress_sql([BotPaths(settings, "claims_chatbot"), BotPaths(settings, "hr_chatbot")])
    assert statement.count("UNION ALL") == 1 and params == {"k0": "claims_chatbot", "k1": "hr_chatbot"}
    assert "`chatbots_test`.`hr_chatbot`.`golden_set`" in statement and "'claims_chatbot'" not in statement
    docs, questions = journey.counts({"bot_id": "x", "reading": "1", "waiting": None, "flagged": "0", "approved": "7",
                                      "questions": "60", "questions_ok": "41"})
    assert docs == {"reading": 1, "waiting": 0, "flagged": 0, "approved": 7} and questions == {"total": 60, "approved": 41}
    assert journey.counts(None) == ({"reading": 0, "waiting": 0, "flagged": 0, "approved": 0}, {"total": 0, "approved": 0})


@pytest.mark.parametrize("error, who, words", [
    (RuntimeError("[TABLE_OR_VIEW_NOT_FOUND] The table or view `qa`.`_platform`.`v_budget` cannot be found."),
     problems.MLOPS, "hasn't been created yet"),
    (PermissionError("User does not have SELECT on table x"), problems.MLOPS, "isn't allowed"),
    (RuntimeError("PERMISSION_DENIED: User does not have USE SCHEMA"), problems.MLOPS, "isn't allowed"),
    (TimeoutError("SQL statement timed out"), problems.WAIT, "took too long"),
    (RuntimeError("TEMPORARILY_UNAVAILABLE: the warehouse is starting"), problems.WAIT, "busy or still starting"),
    (ConnectionError("Max retries exceeded"), problems.WAIT, "couldn't reach Databricks"),
    (RuntimeError("ConcurrentAppendException: files were added"), problems.YOU, "same moment"),
    (KeyError("review_by"), problems.YOU, "unexpected"),
])
def test_errors_are_explained_with_what_to_do(error, who, words):
    problem = problems.describe(error)
    assert problem.who == who and words in problem.what
    assert "Try again" in problem.do or "MLOps" in problem.do
    assert str(error) not in problem.what + problem.do      # the raw text stays in the technical details


def test_every_alert_the_platform_raises_comes_with_advice():
    import pathlib
    import re

    raised = set()
    for path in [*pathlib.Path("src/factory").glob("*.py"), *pathlib.Path("jobs").glob("*.py")]:
        raised |= set(re.findall(r"""\balert\(\s*[\w.\[\]"']+,\s*"([a-z_]+)\"""", path.read_text()))
        raised |= set(re.findall(r"""Alert\(\w+,\s*"([a-z_]+)\"""", path.read_text()))
    assert {"availability", "expiry", "quality_drop", "smoke_failed"} <= raised
    platform_only = {"alert_setup_failed", "integrity", "runtime_publish_failed"}
    assert raised - platform_only <= set(problems.ALERT_ADVICE), raised - platform_only - set(problems.ALERT_ADVICE)
    assert "nothing for you to do" in problems.alert_advice("integrity")
    assert "Documents" in problems.alert_advice("expiry")


def test_every_way_the_quality_check_can_fail_comes_with_advice(settings):
    from factory.evals import ZERO_CHECKS, gate

    thresholds = settings.thresholds()
    result = gate({name: 0.0 for name in thresholds}, thresholds, {check: 2 for check in ZERO_CHECKS},
                  list(ZERO_CHECKS), n_cases=3, min_cases=50, required=("tone",), unscored=4)
    assert len(result.failures) >= len(thresholds) + len(ZERO_CHECKS)
    fallback = problems.check_advice("something new")
    for failure in result.failures:
        assert problems.check_advice(failure) != fallback, failure
    assert "Test questions" in problems.check_advice("Only 3 approved test question(s); at least 50 are required.")
    assert "MLOps" in fallback
