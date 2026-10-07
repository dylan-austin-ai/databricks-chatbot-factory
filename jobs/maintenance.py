"""Nightly maintenance (LCY-5, LCY-6, DOC-11, CAS-6, ADM-6).

* Drift detection: write access or objects in bot schemas the app didn't create.
* Idle bots: no requests in N days -> flag to owner.
* Expired platform pins -> remove pin.
* Soft-deleted bots past their retention window -> drop schema (audit kept).
* Rows older than the retention period (7 years) -> purge.
"""
from _bootstrap import args, context

a = args("catalog", "purge_bot_id", "actor")
spark, sql, settings, cp, w = context(a.catalog)


def purge(bot_id: str, actor: str, reason: dict) -> None:
    """Permanently remove a deleted bot's data: schema (docs, chunks, golden set), index rows,
    auto-ingest trigger job. Audit history, request logs and traces are kept (LCY-6/7)."""
    from factory.provisioning import Provisioner
    sql.execute(f"DROP SCHEMA IF EXISTS `{settings.catalog}`.`{bot_id}` CASCADE")
    sql.execute(f"DELETE FROM {settings.fq('shared_chunks')} WHERE bot_id = :b", {"b": bot_id})
    Provisioner(sql, settings, cp, w).remove_trigger(bot_id)
    cp.set_state(bot_id, "purged", actor, reason)


if a.purge_bot_id:  # admin "Purge now" from the app; only bots already deleted
    bot = cp.get_bot(a.purge_bot_id)
    if not bot or bot["state"] != "deleted":
        raise SystemExit("Only deleted chatbots can be purged.")
    purge(a.purge_bot_id, a.actor or "admin", {"immediate": True})
    raise SystemExit(0)
app_sp = settings.get("access.app_service_principal")
cat = settings.catalog
bots = cp.list_bots()
schemas = [b["bot_id"] for b in bots]

if schemas and app_sp:
    in_list = ", ".join(f":s{i}" for i in range(len(schemas)))
    params = {f"s{i}": s for i, s in enumerate(schemas)}
    params["sp"] = app_sp
    for row in sql.query(
        f"""SELECT table_schema, table_name, grantee, privilege_type
            FROM `{cat}`.information_schema.table_privileges
            WHERE table_schema IN ({in_list}) AND grantee <> :sp
              AND privilege_type IN ('MODIFY', 'ALL PRIVILEGES')""", params):
        cp.alert(row["table_schema"], "drift", "high",
                 f"{row['grantee']} has {row['privilege_type']} on {row['table_name']} "
                 "(bot infrastructure must only be changed through the app).",
                 dedupe_key=f"drift:{row['table_schema']}:{row['table_name']}:{row['grantee']}")
    for row in sql.query(
        f"""SELECT table_schema, table_name, created_by FROM `{cat}`.information_schema.tables
            WHERE table_schema IN ({in_list}) AND created_by <> :sp""", params):
        cp.alert(row["table_schema"], "drift", "high",
                 f"{row['table_name']} was created outside the app by {row['created_by']}.",
                 dedupe_key=f"drift:{row['table_schema']}:{row['table_name']}:created")

idle_days = int(settings.get("idle_days_before_flag", 45))
for row in sql.query(f"""
    SELECT b.bot_id, b.owner_user, max(r.ts) AS last_ts FROM {settings.fq('bots')} b
    LEFT JOIN {settings.fq('request_log')} r ON r.bot_id = b.bot_id AND NOT r.synthetic
    WHERE b.state = 'live' AND b.deleted_at IS NULL GROUP BY b.bot_id, b.owner_user
    HAVING max(r.ts) IS NULL OR max(r.ts) < current_timestamp() - make_interval(0,0,0,{idle_days},0,0,0)"""):
    cp.alert(row["bot_id"], "idle", "low",
             f"No questions in {idle_days}+ days. {row['owner_user']}: consider archiving it.",
             dedupe_key=f"idle:{row['bot_id']}:{__import__('datetime').date.today():%Y-%m}", required=False)

sql.execute(f"""UPDATE {settings.fq('bots')} SET platform_pin = NULL, pin_expires_at = NULL
               WHERE pin_expires_at < current_timestamp()""")

keep_days = int(settings.get("soft_delete_retention_days", 90))
for b in sql.query(f"""SELECT bot_id FROM {settings.fq('bots')} WHERE state = 'deleted'
                      AND deleted_at < current_timestamp() - make_interval(0,0,0,{keep_days},0,0,0)"""):
    purge(b["bot_id"], "system:maintenance", {"after_days": keep_days})

years = int(settings.get("retention_years", 7))
for table in ("request_log", "guardrail_events", "conversations", "judge_results", "feedback",
              "synthetic_health"):
    sql.execute(f"DELETE FROM {settings.fq(table)} WHERE ts < current_timestamp() - INTERVAL {years} YEARS")
sql.execute(f"DELETE FROM {settings.fq('question_embeddings')} WHERE day < current_date() - INTERVAL {years} YEARS")
# MLflow traces in UC (OBS-9): same 7-year retention; trace deletion is row deletion in these tables.
for t, col in (("spans", "start_time_unix_nano"), ("logs", "time_unix_nano")):  # OTel column names
    try:
        sql.execute(f"DELETE FROM {settings.fq('traces_otel_' + t)} "
                    f"WHERE {col} < unix_micros(current_timestamp() - INTERVAL {years} YEARS) * 1000")
    except Exception as e:  # noqa: BLE001 - verify column names against the live tables
        print(f"Trace retention skipped for {t}: {e}")
