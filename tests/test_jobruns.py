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
                      "url": "https://host/run/1", "step": 0, "steps": 0, "task": "", "task_run_id": 0}
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


# --- when a job stops: the real reason, and whether trying again can help -------------------------

WAREHOUSE_ERROR = ("MlflowException: SQL warehouse ID is required for accessing traces in UC tables. Please set the "
                   "MLFLOW_TRACING_SQL_WAREHOUSE_ID environment variable.")


def test_a_stopped_run_points_at_the_task_whose_output_has_the_reason():
    stopped = new("TERMINATED", "RUN_EXECUTION_ERROR", "Task evals failed with message: Workload failed, see run output")
    stopped.tasks = [NS(task_key="ingest", run_id=11, state=None, status=NS(state=NS(value="TERMINATED"),
                                                                             termination_details=NS(code=NS(value="SUCCESS")))),
                     NS(task_key="evals", run_id=13, state=None, status=NS(
                         state=NS(value="TERMINATED"), termination_details=NS(code=NS(value="RUN_EXECUTION_ERROR"))))]
    assert jobruns.snapshot(stopped)["task_run_id"] == 13
    alone = new("TERMINATED", "RUN_EXECUTION_ERROR", "x")
    alone.tasks = [NS(task_key="evals", run_id=21, state=None, status=NS(state=NS(value="TERMINATED"),
                                                                          termination_details=NS(code=NS(value="RUN_EXECUTION_ERROR"))))]
    assert jobruns.snapshot(alone)["task_run_id"] == 21               # a one-task job: that task
    assert jobruns.snapshot(new("TERMINATED", "RUN_EXECUTION_ERROR", "x"))["task_run_id"] == 0
    assert jobruns.snapshot(new("RUNNING"))["task_run_id"] == 0


def test_a_fault_in_the_jobs_setup_is_told_apart_from_something_worth_retrying():
    from factory import problems

    kind, advice = problems.job_stopped(WAREHOUSE_ERROR)
    assert kind == problems.SETUP and "won't help" in advice and "MLOps" in advice
    for message in ("ModuleNotFoundError: No module named 'x'", "PERMISSION_DENIED: User does not have CAN_USE",
                    "TypeError: evaluate() got an unexpected keyword argument 'model_id'"):
        assert problems.job_stopped(message)[0] == problems.SETUP, message
    for message in ("TimeoutError: SQL statement timed out", "503 TEMPORARILY_UNAVAILABLE",
                    "Databricks skipped this run because another run of the same job was still going."):
        kind, advice = problems.job_stopped(message)
        assert kind == problems.TEMPORARY and "Start it again" in advice, message
    kind, advice = problems.job_stopped("Workload failed, see run output for details")
    assert kind == problems.UNCLEAR and "second time" in advice
    assert problems.job_stopped(None)[0] == problems.UNCLEAR


def test_every_job_task_is_given_the_warehouse_and_the_bootstrap_hands_it_to_mlflow(monkeypatch):
    import importlib.util
    import pathlib

    import yaml

    root = pathlib.Path(__file__).resolve().parents[1]
    jobs = yaml.safe_load((root / "resources" / "jobs.yml").read_text())["resources"]["jobs"]
    tasks = [(name, t["task_key"], t["spark_python_task"]["parameters"]) for name, job in jobs.items() for t in job["tasks"]]
    assert len(tasks) >= 20
    for name, key, params in tasks:   # a job is its own process: it knows the warehouse only if it is told
        assert params.count("--warehouse_id=${var.warehouse_id}") == 1, (name, key)

    for var in ("FACTORY_WAREHOUSE_ID", "MLFLOW_TRACING_SQL_WAREHOUSE_ID"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr("sys.argv", ["run_evals.py", "--catalog=qa", "--warehouse_id=abc123", "--bot_id=hr"])
    spec = importlib.util.spec_from_file_location("_bootstrap_under_test", root / "jobs" / "_bootstrap.py")
    boot = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(boot)
    import os

    assert os.environ["FACTORY_WAREHOUSE_ID"] == "abc123"             # read from the task's own parameters
    assert boot.warehouse_for_traces() == "abc123" and os.environ["MLFLOW_TRACING_SQL_WAREHOUSE_ID"] == "abc123"

    for var in ("FACTORY_WAREHOUSE_ID", "MLFLOW_TRACING_SQL_WAREHOUSE_ID"):
        monkeypatch.delenv(var, raising=False)
    assert boot.warehouse_for_traces("saved99") == "saved99"          # a job copy made before the parameter existed
    assert os.environ["MLFLOW_TRACING_SQL_WAREHOUSE_ID"] == "saved99"
    for var in ("FACTORY_WAREHOUSE_ID", "MLFLOW_TRACING_SQL_WAREHOUSE_ID"):
        monkeypatch.delenv(var, raising=False)
    assert boot.warehouse_for_traces("") == "" and "MLFLOW_TRACING_SQL_WAREHOUSE_ID" not in os.environ


# --- a quality check that can actually finish ---------------------------------------------------------

def test_runs_that_did_nothing_do_not_set_the_usual_time():
    t0 = 1_760_000_000_000
    quick = [new("TERMINATED", "SUCCESS", start=t0, end=t0 + 40_000) for _ in range(6)]      # nothing to check
    real = [new("TERMINATED", "SUCCESS", start=t0, end=t0 + 22 * MIN)]
    assert jobruns.typical_seconds(quick, default=900) == 900            # not "about 1 minute"
    assert jobruns.typical_seconds(quick + real, default=900) == 22 * 60


def test_the_check_is_throttled_and_gives_up_on_a_silent_judge(settings):
    from factory import evals

    limits = evals.check_limits(settings)
    assert limits == {"MLFLOW_GENAI_EVAL_MAX_WORKERS": "4", "MLFLOW_GENAI_EVAL_MAX_SCORER_WORKERS": "2",
                      "MLFLOW_GENAI_EVAL_SCORER_RATE_LIMIT": "2", "MLFLOW_GENAI_EVAL_LLM_TIMEOUT": "60",
                      "MLFLOW_GATEWAY_ROUTE_TIMEOUT_SECONDS": "60"}
    settings._data["quality"]["check"] = {"questions_at_once": 8, "judge_calls_per_second": 0.5}
    tuned = evals.check_limits(settings)
    assert tuned["MLFLOW_GENAI_EVAL_MAX_WORKERS"] == "8" and tuned["MLFLOW_GENAI_EVAL_SCORER_RATE_LIMIT"] == "0.5"
    assert tuned["MLFLOW_GENAI_EVAL_MAX_SCORER_WORKERS"] == "2"          # unset values keep their default


def test_the_judge_model_is_asked_one_question_before_a_check_starts():
    from factory import evals

    asked = []
    ok = NS(chat=NS(completions=NS(create=lambda **k: asked.append(k) or NS())))
    assert evals.judge_problem(ok, "databricks-claude-haiku-4-5") == ""
    assert asked[0]["model"] == "databricks-claude-haiku-4-5" and asked[0]["timeout"] == 60 and asked[0]["max_tokens"] == 5

    def refuse(**k):
        raise PermissionError("PERMISSION_DENIED: principal lacks CAN_QUERY on endpoint\nrequest id 123\nstack...")

    reason = evals.judge_problem(NS(chat=NS(completions=NS(create=refuse))), "x")
    assert reason == "PermissionError: PERMISSION_DENIED: principal lacks CAN_QUERY on endpoint"


def test_a_run_someone_stopped_is_not_called_an_error():
    from factory import problems

    now = 100 * MIN
    stopped = RunStatus(FAILED, 1, ended_ms=now - MIN, message="USER_CANCELED")
    assert jobruns.sentence(stopped, "quality check", now, 0) == "The last quality check was stopped by someone 1 minute ago."
    kind, advice = problems.job_stopped("Run cancelled by user (USER_CANCELED)")
    assert kind == problems.STOPPED_BY_HAND and "Start it again" in advice
    kind, _ = problems.job_stopped("The judge model 'x' did not answer a one-line test, so nothing was checked")
    assert kind == problems.SETUP


def test_each_question_of_the_check_is_one_trace_and_the_limits_come_first():
    import pathlib

    source = (pathlib.Path(__file__).resolve().parents[1] / "jobs" / "run_evals.py").read_text()
    predict = source[source.index("def make_predict("):source.index("def read_scores(")]
    assert '@mlflow.trace(name="quality_check_question")\n    def predict(question: str)' in predict
    assert predict.index("@mlflow.trace") < predict.index("mlflow.start_span") < predict.index("update_current_trace")
    assert source.index("evals.check_limits(settings)") < source.index("evals.judge_problem(") \
        < source.index("mlflow.genai.evaluate(")


# --- which questions a check uses, and the diagnostic run -----------------------------------------------

def _case(i, doc, kind="in_scope"):
    return {"eval_id": f"e{i:03d}", "question": f"Question {i}?", "kind": kind,
            "source_doc_id": doc if kind == "in_scope" else None}


def test_a_big_question_set_is_checked_on_a_fixed_sample_spread_over_the_documents():
    from factory import evals

    cases = [_case(i, f"doc{i % 104}") for i in range(290)] + [_case(300 + i, None, "out_of_scope") for i in range(18)] \
        + [_case(400 + i, None, "feedback") for i in range(4)]
    assert len(cases) == 312
    sample = evals.sample_cases(cases, 50)
    assert len(sample) == 50 and evals.sample_cases(cases, 50) == sample          # the same ones every time
    assert evals.sample_cases(list(reversed(cases)), 50) == [c for c in reversed(cases) if c in sample]   # whatever the order
    kinds = [c["kind"] for c in sample]
    assert kinds.count("out_of_scope") == 5 and kinds.count("feedback") == 4      # rare kinds keep a handful
    docs = [c["source_doc_id"] for c in sample if c["kind"] == "in_scope"]
    assert len(docs) == 41 and len(set(docs)) == 41                               # one per document, no repeats
    assert [c["eval_id"] for c in sample] == sorted(c["eval_id"] for c in sample)  # in the order they came
    assert evals.sample_cases(cases[:40], 50) == cases[:40] and evals.sample_cases(cases, 0) == cases   # 0 = all


def test_each_diagnostic_step_reports_how_it_went(capsys):
    import time

    from factory import evals

    assert evals.timed("a quick step", lambda: 7, 5) == (True, 7)

    def broken():
        raise PermissionError("PERMISSION_DENIED: no CAN_USE on warehouse\nmore detail")

    assert evals.timed("a step that fails", broken, 5) == (False, None)
    assert evals.timed("a step that hangs", lambda: time.sleep(3), 1) == (False, None)
    said = capsys.readouterr().out.splitlines()
    assert said[0].startswith("DIAG: a quick step: ok in ")
    assert said[1].startswith("DIAG: a step that fails: FAILED in ") and said[1].endswith(
        "PermissionError: PERMISSION_DENIED: no CAN_USE on warehouse")
    assert said[2] == "DIAG: a step that hangs: STILL WAITING after 1s (this is where it stalls)"


def test_the_diagnostic_run_is_wired_into_the_job_and_enough_questions_counts_the_approved_ones():
    import pathlib

    import yaml

    root = pathlib.Path(__file__).resolve().parents[1]
    source = (root / "jobs" / "run_evals.py").read_text()
    assert 'os.environ["MLFLOW_GENAI_EVAL_MAX_WORKERS"] = os.environ["MLFLOW_GENAI_EVAL_MAX_SCORER_WORKERS"] = "1"' in source
    assert "faulthandler.dump_traceback_later(90, repeat=True" in source and "cancel_dump_traceback_later()" in source
    for step in ("1. the chatbot answers one question", "4. that trace is read back by its id",
                 "6. recent traces are searched", "7. the judge model answers one question"):
        assert step in source, step
    # sampling must not make "at least 50 approved questions" fail: the rule counts what is approved
    assert "n_in_scope if PARTIAL else approved_in_scope" in source
    assert source.index("approved_in_scope = sum(") < source.index("evals.sample_cases(cases")
    job = yaml.safe_load((root / "resources" / "jobs.yml").read_text())["resources"]["jobs"]["run_evals"]
    assert {"limit", "diagnose"} <= {p["name"] for p in job["parameters"]}
    params = job["tasks"][0]["spark_python_task"]["parameters"]
    assert "--limit={{job.parameters.limit}}" in params and "--diagnose={{job.parameters.diagnose}}" in params
    done = new("TERMINATED", "SUCCESS", start=1, end=2)
    done.tasks = [NS(task_key="evals", run_id=55, state=None, status=done.status)]
    assert jobruns.snapshot(done)["task_run_id"] == 55                 # so its output can be read afterwards
