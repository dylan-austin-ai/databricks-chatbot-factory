"""Background jobs as the app sees them: not starting the same one twice, and saying truthfully
where a run stands.

Some jobs only need to run once however many times they are asked for: rebuilding a chatbot's
test version picks up every change made before it starts. The ingest job runs one at a time for
the whole platform, so asking for it after each of twenty approvals would queue twenty runs, and
every other chatbot would wait behind them. If an identical run is still waiting to start, the
new request is already covered by it.

A page must never say "it's running" because a button was pressed. It says so because Databricks
says so: `snapshot` reads a run's real state, and the rest turns that into the sentence, the
progress and the "about how long" that a person waiting needs.
"""
from __future__ import annotations

from dataclasses import dataclass
from statistics import median

NOT_STARTED = {"QUEUED", "PENDING", "BLOCKED", "WAITING", "WAITING_FOR_RETRY"}
GOING = {"RUNNING", "TERMINATING"}
NONE, WAITING, RUNNING, SUCCEEDED, FAILED, UNKNOWN = "none", "waiting", "running", "succeeded", "failed", "unknown"


def _state(run) -> str:
    """The run's state as text, from either shape the Jobs API returns."""
    for holder, field in ((getattr(run, "status", None), "state"), (getattr(run, "state", None), "life_cycle_state")):
        value = getattr(holder, field, None)
        if value is not None:
            return str(getattr(value, "value", value)).upper()
    return ""


def _parameters(run) -> dict[str, str]:
    return {p.name: str(p.value if p.value is not None else p.default or "")
            for p in (getattr(run, "job_parameters", None) or []) if getattr(p, "name", None)}


def covered_by(runs, params: dict[str, str]) -> int | None:
    """The run_id of a run that hasn't started yet and was asked for with exactly `params`, or
    None. A run that is already going doesn't count: it may have read the data before the
    change."""
    wanted = {k: str(v) for k, v in params.items()}
    for run in runs:
        if _state(run) not in NOT_STARTED:
            continue
        have = _parameters(run)
        if all(have.get(k, "") == v for k, v in wanted.items()) and \
                all(v in ("", "false") or k in wanted for k, v in have.items()):
            return getattr(run, "run_id", None)
    return None


# Where a run stands ------------------------------------------------------------------------------

@dataclass(frozen=True)
class RunStatus:
    state: str = NONE        # none | waiting | running | succeeded | failed | unknown (can't be read)
    run_id: int | None = None
    asked_ms: int = 0        # when someone pressed the button
    started_ms: int = 0      # when Databricks started running it (0 while it waits)
    ended_ms: int = 0
    message: str = ""        # Databricks' own words when it stopped with an error
    url: str = ""            # the run's page in Databricks, for MLOps
    by: str = ""             # who started it
    step: int = 0            # for a job made of several tasks: the one it is on, or stopped at (1-based)
    steps: int = 0           # how many tasks the job has
    task: str = ""           # that task's key
    task_run_id: int = 0     # for a run that stopped: the task run whose output holds the real error

    @property
    def active(self) -> bool:
        return self.state in (WAITING, RUNNING)


def _word(value) -> str:
    return str(getattr(value, "value", value) or "").upper()


# What each task of the factory's jobs is doing, in words for the people waiting on it.
TASK_WHAT = {"provision": "setting up the chatbot", "ingest": "updating the documents and sections",
             "compare_strategies": "choosing the search method", "evals": "the quality check",
             "first_eval": "the quality check"}


def _stage(run, state: str) -> tuple[int, int, str]:
    """Which task a multi-task run is on: (position, how many, task key). For a run that stopped
    it is the task that failed; for one still going, the first task that hasn't finished."""
    tasks = list(getattr(run, "tasks", None) or [])
    if len(tasks) < 2:
        return 0, len(tasks), ""
    states = [_one(t)["state"] for t in tasks]
    wanted = (FAILED,) if state == FAILED else (RUNNING, WAITING)
    for want in wanted:
        if want in states:
            i = states.index(want)
            return i + 1, len(tasks), str(getattr(tasks[i], "task_key", "") or "")
    return 0, len(tasks), ""


def snapshot(run) -> dict:
    """A Jobs API run reduced to plain values: state, start and end (ms), message, page URL and,
    for a job made of several tasks, which one it is on. Handles both shapes the API returns (the
    newer `status` and the older `state`)."""
    snap = _one(run)
    snap["step"], snap["steps"], snap["task"] = _stage(run, snap["state"])
    snap["task_run_id"] = 0
    tasks = list(getattr(run, "tasks", None) or [])
    if snap["state"] == FAILED:   # the run only says "see run output"; the task that failed has the reason
        failed = [t for t in tasks if _one(t)["state"] == FAILED] or tasks[:1]
        snap["task_run_id"] = int(getattr(failed[0], "run_id", 0) or 0) if failed else 0
    elif snap["state"] == SUCCEEDED and len(tasks) == 1:   # what it printed can be read from here
        snap["task_run_id"] = int(getattr(tasks[0], "run_id", 0) or 0)
    return snap


def _one(run) -> dict:
    life = _state(run)
    details = getattr(getattr(run, "status", None), "termination_details", None)
    old = getattr(run, "state", None)
    code, result = _word(getattr(details, "code", None)), _word(getattr(old, "result_state", None))
    message = getattr(details, "message", None) or getattr(old, "state_message", None) or ""
    if life in NOT_STARTED:
        state = WAITING
    elif life in GOING:
        state = RUNNING
    elif life == "SKIPPED":
        state, message = FAILED, "Databricks skipped this run because another run of the same job was still going."
    elif life == "INTERNAL_ERROR" or code not in ("", "SUCCESS") or result not in ("", "SUCCESS"):
        state, message = FAILED, message or code or result or life
    elif life:
        state = SUCCEEDED
    else:
        state = UNKNOWN
    return {"state": state, "started_ms": int(getattr(run, "start_time", 0) or 0),
            "ended_ms": int(getattr(run, "end_time", 0) or 0), "message": str(message)[:500],
            "url": getattr(run, "run_page_url", None) or ""}


def typical_seconds(runs, default: int) -> int:
    """How long this job usually takes: the middle duration of its recent successful runs, or
    `default` when there is no history yet. Runs that ended in a fraction of the default are left
    out: they found nothing to do (no test version, too few approved questions), and counting
    them makes a 15-minute job look like a 1-minute one."""
    took = []
    for run in runs:
        snap = snapshot(run)
        seconds = (snap["ended_ms"] - snap["started_ms"]) / 1000
        if snap["state"] == SUCCEEDED and snap["started_ms"] > 0 and seconds >= max(60, default * 0.2):
            took.append(seconds)
    return int(median(took)) if took else default


def span(seconds: float) -> str:
    """A length of time the way people say it: "under a minute", "6 minutes", "1 hour 5 minutes",
    "3 days"."""
    minutes = int(seconds // 60)
    if minutes < 1:
        return "under a minute"
    if minutes < 60:
        return f"{minutes} minute{'' if minutes == 1 else 's'}"
    hours, rest = divmod(minutes, 60)
    if hours >= 48:
        return f"{hours // 24} days"
    return f"{hours} hour{'' if hours == 1 else 's'}" + (f" {rest} minute{'' if rest == 1 else 's'}" if rest else "")


def progress(status: RunStatus, now_ms: float, typical_s: int) -> float:
    """How far along to draw the bar, from 0 to 1. A running job is measured against how long the
    job usually takes and never shows as finished before it is."""
    if status.state == RUNNING and status.started_ms:
        return max(0.02, min(0.95, (now_ms - status.started_ms) / 1000 / max(typical_s, 1)))
    return 1.0 if status.state in (SUCCEEDED, FAILED) else 0.0


def sentence(status: RunStatus, what: str, now_ms: float, typical_s: int) -> str:
    """One plain sentence about where a run stands. `what` names the job, e.g. "quality check"."""
    who = f" by {status.by}" if status.by else ""
    stage = (f"step {status.step} of {status.steps} ({TASK_WHAT.get(status.task, status.task.replace('_', ' '))})"
             if status.step and status.steps > 1 else "")
    if status.state == WAITING:
        waited = span((now_ms - status.asked_ms) / 1000) if status.asked_ms else "a moment"
        return (f"The {what} was started{who} and is waiting its turn ({waited} so far). It begins as soon as "
                "Databricks has a machine ready or the run ahead of it finishes.")
    if status.state == RUNNING:
        elapsed = (now_ms - (status.started_ms or status.asked_ms)) / 1000
        slow = " This one is taking longer than usual." if elapsed > typical_s * 1.5 else ""
        return (f"The {what} is running: {stage + ', ' if stage else ''}{span(elapsed)} so far. It usually takes "
                f"about {span(typical_s)}.{slow}")
    when = span(max(0, now_ms - (status.ended_ms or status.asked_ms)) / 1000) + " ago"
    if status.state == FAILED:
        how = "was stopped by someone" if "CANCEL" in status.message.upper() else "stopped with an error"
        return f"The last {what} {how} {when}{', at ' + stage if stage else ''}."
    if status.state == SUCCEEDED:
        return f"The last {what} finished {when}."
    if status.state == UNKNOWN:
        return f"A {what} was started{who} {when}, but the app can't read how it is going."
    return ""
