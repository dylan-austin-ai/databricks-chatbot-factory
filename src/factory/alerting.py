"""Email delivery for alerts through Databricks SQL alerts (OBS-5).

The monitor job evaluates the rules and writes rows to _platform.alerts. Each bot gets
two SQL alerts over its own rows, subscribed by email:
  * business hours (every 15 minutes, 7:00-20:00 Central, weekdays): all kinds the owner
    receives (required + the optional ones they kept on)
  * after hours (hourly): required kinds only
Platform-wide alerts (bot_id NULL) and every chatbot's budget alerts go to MLOps.

Scheduling SQL alerts every 15 minutes keeps the SQL warehouse awake during business
hours; use a small dedicated serverless warehouse with a 1-minute auto-stop.
"""
from __future__ import annotations

from .config import BotConfig, PlatformSettings
from .monitoring import REQUIRED_KINDS
from .sql import ident

BUSINESS_CRON = "0 0/15 7-20 ? * MON-FRI"
AFTER_HOURS_CRON = "0 5 * * * ?"
TZ = "America/Chicago"


def alert_query(settings: PlatformSettings, bot_id: str | None, kinds: set[str] | None,
                minutes: int) -> str:
    if bot_id is not None:
        ident(bot_id)  # validated identifier, safe to inline
    # Budgets are MLOps' job (WIZ-13): budget alerts go to the platform alert, never the owner's.
    where = "(bot_id IS NULL OR kind = 'budget')" if bot_id is None else f"bot_id = '{bot_id}' AND kind <> 'budget'"
    kind_filter = "" if not kinds else " AND kind IN (" + ", ".join(f"'{k}'" for k in sorted(kinds)) + ")"
    return (f"SELECT count(*) AS n, concat_ws('\\n', collect_list(concat(kind, ': ', message))) AS messages "
            f"FROM {settings.fq('alerts')} WHERE {where} AND NOT resolved{kind_filter} "
            f"AND ts > current_timestamp() - INTERVAL {minutes} MINUTES")


def recipients(cfg: BotConfig | None, settings: PlatformSettings) -> list[str]:
    mlops = list(settings.get("monitoring.alert_emails_mlops", []))
    if cfg is None:
        return mlops
    return sorted({cfg.owner_user, *mlops} - {""})


def ensure_bot_alerts(w, settings: PlatformSettings, cfg: BotConfig | None, warehouse_id: str) -> list[str]:
    """Create or update the bot's two SQL alerts. Returns alert names."""
    from databricks.sdk.service import sql as dsql

    bot_id = cfg.bot_id if cfg else None
    label = bot_id or "platform"
    owner_kinds = None if cfg is None or cfg.alert_prefs.get("traffic", True) else \
        (REQUIRED_KINDS | {"guardrail_spike", "idle"})
    specs = [(f"[chatbot] {label} alerts", alert_query(settings, bot_id, owner_kinds, 20), BUSINESS_CRON),
             (f"[chatbot] {label} after-hours", alert_query(settings, bot_id, REQUIRED_KINDS, 65),
              AFTER_HOURS_CRON)]
    subs = [dsql.AlertV2Subscription(user_email=e) for e in recipients(cfg, settings)]
    existing = {a.display_name: a for a in w.alerts_v2.list_alerts()}
    for name, query, cron in specs:
        alert = dsql.AlertV2(
            display_name=name, query_text=query, warehouse_id=warehouse_id,
            custom_summary=f"Chatbot alert: {label}",
            custom_description="{{QUERY_RESULT_TABLE}}",
            evaluation=dsql.AlertV2Evaluation(
                source=dsql.AlertV2OperandColumn(name="n"),
                comparison_operator=dsql.ComparisonOperator.GREATER_THAN,
                threshold=dsql.AlertV2Operand(value=dsql.AlertV2OperandValue(double_value=0)),
                notification=dsql.AlertV2Notification(subscriptions=subs, notify_on_ok=False)),
            schedule=dsql.CronSchedule(quartz_cron_schedule=cron, timezone_id=TZ))
        if name in existing:
            w.alerts_v2.update_alert(existing[name].id, alert, update_mask="*")
        else:
            w.alerts_v2.create_alert(alert)
    return [s[0] for s in specs]
