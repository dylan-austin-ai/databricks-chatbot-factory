"""Not starting the same background job twice.

Some jobs only need to run once however many times they are asked for: rebuilding a chatbot's
test version picks up every change made before it starts. The ingest job runs one at a time for
the whole platform, so asking for it after each of twenty approvals would queue twenty runs, and
every other chatbot would wait behind them. If an identical run is still waiting to start, the
new request is already covered by it.
"""
from __future__ import annotations

NOT_STARTED = {"QUEUED", "PENDING", "BLOCKED", "WAITING"}


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
