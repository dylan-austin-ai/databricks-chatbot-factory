"""Monitoring rules (OBS-1..5, CST-8, DCL-2). Pure functions; the monitor job feeds them.

Required alerts (owners can't turn them off): availability, latency, error rate,
harmful output, budget, document expiry, smoke-test and integrity failures.
Optional alerts (per-bot preference): unexpected high/low traffic.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

REQUIRED_KINDS = {"availability", "latency", "error_rate", "harmful_output", "budget", "expiry",
                  "smoke_failed", "integrity", "eval_regression", "quality_drop"}


@dataclass
class Alert:
    bot_id: str | None
    kind: str
    severity: str
    message: str
    dedupe_key: str

    @property
    def required(self) -> bool:
        return self.kind in REQUIRED_KINDS


def _parse_window_edge(spec: str) -> tuple[int, int, ZoneInfo]:
    clock, tz = spec.split()
    hh, mm = (int(x) for x in clock.split(":"))
    return hh, mm, ZoneInfo(tz)


def in_traffic_window(ts: datetime, window: dict) -> bool:
    """OBS-3: e.g. 08:00 America/New_York through 18:00 America/Los_Angeles, weekdays."""
    ts = ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    sh, sm, stz = _parse_window_edge(window["start"])
    eh, em, etz = _parse_window_edge(window["end"])
    local_start_day = ts.astimezone(stz)
    if local_start_day.weekday() >= 5:
        return False
    start = local_start_day.replace(hour=sh, minute=sm, second=0, microsecond=0)
    end_local = ts.astimezone(etz).replace(hour=eh, minute=em, second=0, microsecond=0)
    return start <= ts <= end_local


def window_key(ts: datetime) -> str:
    return ts.strftime("%Y-%m-%dT%H:%M")


def evaluate_window(row: dict, settings_monitoring: dict, prefs: dict, baseline: float | None,
                    window_start: datetime) -> list[Alert]:
    """row: one monitoring_rollup row for a bot and 15-minute window."""
    m = settings_monitoring
    bot, key = row["bot_id"], window_key(window_start)
    out: list[Alert] = []
    req = int(row.get("requests") or 0)
    if row.get("availability") is not None and float(row["availability"]) < m["availability_target"]:
        out.append(Alert(bot, "availability", "high",
                         f"Availability was {float(row['availability']):.1%} over 15 minutes "
                         f"(target {m['availability_target']:.0%}).", f"availability:{bot}:{key}"))
    for pct in ("p50", "p95", "p99"):
        val = row.get(f"{pct}_ms")
        limit = m[f"latency_{pct}_s"] * 1000
        if req and val is not None and int(val) > limit:
            out.append(Alert(bot, "latency", "medium",
                             f"{pct} latency was {int(val) / 1000:.1f}s (limit {limit / 1000:.0f}s).",
                             f"latency:{pct}:{bot}:{key}"))
    ttft, ttft_limit = row.get("p95_ttft_ms"), m.get("ttft_p95_s", 4) * 1000
    if req and ttft is not None and int(ttft) > ttft_limit:
        out.append(Alert(bot, "latency", "medium",
                         f"p95 time to first token was {int(ttft) / 1000:.1f}s (limit {ttft_limit / 1000:.0f}s).",
                         f"latency:ttft:{bot}:{key}"))
    if req and int(row.get("errors") or 0) / req > m["error_rate_max"]:
        out.append(Alert(bot, "error_rate", "high",
                         f"{int(row['errors'])} of {req} requests failed in 15 minutes "
                         f"(limit {m['error_rate_max']:.0%}).", f"error_rate:{bot}:{key}"))
    if int(row.get("harmful_blocks") or 0):
        out.append(Alert(bot, "harmful_output", "high",
                         f"{int(row['harmful_blocks'])} harmful or policy-violating output(s) were blocked.",
                         f"harmful:{bot}:{key}"))
    if prefs.get("traffic", True) and baseline and in_traffic_window(window_start, m["traffic_window"]):
        if req < baseline * m["traffic_low_ratio"]:
            out.append(Alert(bot, "traffic_low", "low",
                             f"Only {req} questions in 15 minutes; usually about {baseline:.0f}.",
                             f"traffic_low:{bot}:{key}"))
        elif req > baseline * m["traffic_high_ratio"]:
            out.append(Alert(bot, "traffic_high", "medium",
                             f"{req} questions in 15 minutes; usually about {baseline:.0f}.",
                             f"traffic_high:{bot}:{key}"))
    return out


def budget_alerts(bot_id: str, month_cost: float, budget: float, levels: list[float],
                  month: str) -> list[Alert]:
    """CST-8: alerts at 70/90/100% of the monthly budget, once per level per month."""
    out = []
    for lvl in sorted(levels):
        if budget > 0 and month_cost >= budget * lvl:
            out.append(Alert(bot_id, "budget", "high" if lvl >= 1 else "medium",
                             f"This chatbot has used ${month_cost:,.2f} of its ${budget:,.0f} monthly "
                             f"budget ({month_cost / budget:.0%}).", f"budget:{bot_id}:{month}:{lvl}"))
    return out


def should_pause_for_budget(month_cost: float, budget: float, action: str, state: str) -> bool:
    return action == "pause" and budget > 0 and month_cost >= budget and state == "live"


def expiry_notices(bot_id: str, docs: list[dict], lead_days: int, today: date) -> list[Alert]:
    """DCL-2: one notice per document version, lead_days before it expires."""
    out = []
    for d in docs:
        exp = d.get("expires_at")
        if not exp or d.get("no_expiry"):
            continue
        exp_day = exp.date() if isinstance(exp, datetime) else date.fromisoformat(str(exp)[:10])
        days = (exp_day - today).days
        if 0 <= days <= lead_days:
            out.append(Alert(bot_id, "expiry", "medium",
                             f"\"{d['doc_name']}\" expires on {exp_day:%b %d, %Y} ({days} days). Upload a "
                             "new version or extend the date, or it will stop being used.",
                             f"expiry:{bot_id}:{d['doc_id']}:{d['doc_version']}"))
    return out

