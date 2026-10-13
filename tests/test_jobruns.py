"""Where a background job's run stands, read from Databricks and said in plain words."""
from types import SimpleNamespace as NS

from factory import jobruns, journey
from factory.jobruns import FAILED, RUNNING, SUCCEEDED, UNKNOWN, WAITING, RunStatus

MIN = 60_000


def new(state, code=None, message=None, start=0, end=0):
    details = NS(code=NS(value=code), message=message) if code else None
    return NS(status=NS(state=NS(value=state), termination_details=details), state=None, start_time=start,
              end_time=end, run_page_url="https://host/run/1")


def old(life, result=None, message=None, start=0, end=0):
    return NS(status=None, state=NS(life_cycle_state=NS(value=life), result_state=NS(value=result) if result else None,
                                    state_message=message), start_time=start, end_time=end, run_page_url=None)


def test_a_run_is_read_the_same_from_either_shape_the_api_returns():
    for run, state in [(new("QUEUED"), WAITING), (new("PENDING"), WAITING), (old("PENDING"), WAITING),
                       (new("RUNNING"), RUNNING), (old("RUNNING"), RUNNING), (new("TERMINATING"), RUNNING),
                       (new("TERMINATED", "SUCCESS"), SUCCEEDED), (old("TERMINATED", "SUCCESS"), SUCCEEDED),
                       (new("TERMINATED", "RUN_EXECUTION_ERROR", "boom"), FAILED),
                       (old("TERMINATED", "FAILED", "boom"), FAILED), (old("TERMINATED", "TIMEDOUT"), FAILED),
                       (new("TERMINATED", "USER_CANCELED"), FAILED), (old("INTERNAL_ERROR"), FAILED),
                       (old("SKIPPED"), FAILED), (NS(), UNKNOWN)]:
        assert jobruns.snapshot(run)["state"] == state, run
    failed = jobruns.snapshot(new("TERMINATED", "RUN_EXECUTION_ERROR", "Task failed: x", start=5, end=9))
    assert failed == {"state": FAILED, "started_ms": 5, "ended_ms": 9, "message": "Task failed: x",
                      "url": "https://host/run/1", "step": 0, "steps": 0, "task": ""}
    assert "another run of the same job" in jobruns.snapshot(old("SKIPPED"))["message"]
    assert len(jobruns.snapshot(old("TERMINATED", "FAILED", "x" * 5000))["message"]) == 500


def test_how_long_a_job_usually_takes_comes_from_its_own_history():
    t0 = 1_760_000_000_000                                              # some moment, in milliseconds
    runs = [new("TERMINATED", "SUCCESS", start=t0, end=t0 + 10 * MIN), new("TERMINATED", "SUCCESS", start=t0, end=t0 + 14 * MIN),
            new("TERMINATED", "SUCCESS", start=t0, end=t0 + 30 * MIN),
            new("TERMINATED", "RUN_EXECUTION_ERROR", start=t0, end=t0 + MIN),   # a failure says nothing about how long it takes
            new("RUNNING", start=t0), new("TERMINATED", "SUCCESS", start=0, end=0)]
    assert jobruns.typical_seconds(runs, default=900) == 14 * 60        # the middle successful run
    assert jobruns.typical_seconds([], default=900) == 900              # no history yet: a first guess


def test_time_is_said_the_way_people_say_it():
    assert [jobruns.span(s) for s in (5, 60, 6 * 60 + 10, 3600, 65 * 60, 2 * 3600 + 60)] == [
        "under a minute", "1 minute", "6 minutes", "1 hour", "1 hour 5 minutes", "2 hours 1 minute"]


def test_progress_is_measured_against_the_usual_time_and_never_finishes_early():
    now = 100 * MIN
    running = RunStatus(RUNNING, 1, asked_ms=now - 7 * MIN, started_ms=now - 6 * MIN)
    assert abs(jobruns.progress(running, now, 15 * 60) - 0.4) < 0.01
    assert jobruns.progress(RunStatus(RUNNING, 1, started_ms=now - 60 * MIN), now, 15 * 60) == 0.95   # overdue
    assert jobruns.progress(RunStatus(RUNNING, 1, started_ms=now - 1000), now, 15 * 60) == 0.02      # just started
    assert jobruns.progress(RunStatus(WAITING, 1, asked_ms=now), now, 900) == 0.0
    assert jobruns.progress(RunStatus(SUCCEEDED, 1), now, 900) == 1.0 and jobruns.progress(RunStatus(), now, 900) == 0.0
    assert RunStatus(WAITING).active and RunStatus(RUNNING).active and not RunStatus(FAILED).active
    assert not RunStatus().active and not RunStatus(UNKNOWN).active


def test_each_state_has_a_plain_sentence():
    now = 100 * MIN
    say = lambda status, typical=900: jobruns.sentence(status, "quality check", now, typical)  # noqa: E731
    assert say(RunStatus(RUNNING, 1, started_ms=now - 6 * MIN)) == (
        "The quality check is running: 6 minutes so far. It usually takes about 15 minutes.")
    assert say(RunStatus(RUNNING, 1, started_ms=now - 40 * MIN)).endswith("This one is taking longer than usual.")
    waiting = say(RunStatus(WAITING, 1, asked_ms=now - 3 * MIN, by="ana@corp.com"))
    assert "started by ana@corp.com" in waiting and "waiting its turn (3 minutes so far)" in waiting
    assert say(RunStatus(FAILED, 1, ended_ms=now - 2 * MIN)) == "The last quality check stopped with an error 2 minutes ago."
    assert say(RunStatus(SUCCEEDED, 1, ended_ms=now - 90 * MIN)) == "The last quality check finished 1 hour 30 minutes ago."
    assert "can't read how it is going" in say(RunStatus(UNKNOWN, 1, asked_ms=now - MIN))
    assert say(RunStatus()) == ""


def test_a_running_check_is_nobodys_turn():
    args = ("testing", {"approved": 4}, {"total": 60, "approved": 55}, 50, {"release_id": "r1"}, False)
    idle = journey.steps(*args)[4]
    busy = journey.steps(*args, checking=True)[4]
    assert idle.status == journey.NOW and "press **Run the quality check**" in idle.detail
    assert busy.status == journey.WAITING and "is running" in busy.detail and busy.page == "launch"
    passed = journey.steps("testing", {"approved": 4}, {"total": 60, "approved": 55}, 50,
                           {"release_id": "r1", "eval_passed": "true"}, False, checking=True)[4]
    assert passed.status == journey.DONE          # a re-run in progress doesn't undo a pass


def task(key, state, code=None):
    details = NS(code=NS(value=code)) if code else None
    return NS(task_key=key, status=NS(state=NS(value=state), termination_details=details), state=None)


def test_a_job_made_of_several_tasks_says_which_one_it_is_on():
    going = new("RUNNING", start=1)
    going.tasks = [task("ingest", "TERMINATED", "SUCCESS"), task("compare_strategies", "RUNNING"), task("evals", "BLOCKED")]
    snap = jobruns.snapshot(going)
    assert (snap["state"], snap["step"], snap["steps"], snap["task"]) == (RUNNING, 2, 3, "compare_strategies")
    now = 100 * MIN
    said = jobruns.sentence(RunStatus(RUNNING, 1, started_ms=now - 6 * MIN, step=3, steps=3, task="evals"),
                            "update of the test version", now, 17 * 60)
    assert said == ("The update of the test version is running: step 3 of 3 (the quality check), 6 minutes so far. "
                    "It usually takes about 17 minutes.")

    stopped = new("TERMINATED", "RUN_EXECUTION_ERROR", "Task evals failed", start=1, end=2)
    stopped.tasks = [task("ingest", "TERMINATED", "SUCCESS"), task("compare_strategies", "TERMINATED", "SUCCESS"),
                     task("evals", "TERMINATED", "RUN_EXECUTION_ERROR")]
    snap = jobruns.snapshot(stopped)
    assert (snap["state"], snap["step"], snap["task"]) == (FAILED, 3, "evals")
    assert jobruns.sentence(RunStatus(FAILED, 1, ended_ms=now - 2 * MIN, step=3, steps=3, task="evals"), "update", now, 0) \
        == "The last update stopped with an error 2 minutes ago, at step 3 of 3 (the quality check)."

    single = new("RUNNING", start=1)
    single.tasks = [task("evals", "RUNNING")]
    assert jobruns.snapshot(single)["step"] == 0                       # one task: no "step 1 of 1"
    assert jobruns.span(3 * 86400 + 5) == "3 days" and jobruns.span(30 * 3600) == "30 hours"
