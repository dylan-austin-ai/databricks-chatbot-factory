"""Monitor job (OBS-1..5, PRV-1/2, CNV-1, CST-7/8, DCL-2).

Runs every 15 minutes during business hours and hourly otherwise:
  1. Load agent request logs and app health probes from the restricted logs volume
     into request_log, guardrail_events, conversations (saved-history bots) and
     synthetic_health.
  2. Compute 15-minute rollups and evaluate alert rules.
  3. Hourly: send a canary question to every live bot.
  4. Daily: variable cost per bot, budget alerts / pause / resume, expiry notices.
"""
import json
from datetime import datetime, timedelta, timezone

from _bootstrap import args, context

from factory import evals, llm, monitoring
from factory.ingestion import BotPaths
from factory.lifecycle import State
from factory.runtime import logs_dir

a = args("catalog", "force_daily")
spark, sql, settings, cp, w = context(a.catalog)
s = settings
mon = s.get("monitoring")
now = datetime.now(timezone.utc)
LOGS = logs_dir(s)

# 1. Load logs ---------------------------------------------------------------------------
sql.execute(f"CREATE TABLE IF NOT EXISTS {s.fq('request_events')}")
sql.execute(f"""COPY INTO {s.fq('request_events')} FROM '{LOGS}/requests'
                FILEFORMAT = JSON FORMAT_OPTIONS ('inferSchema' = 'true', 'mergeSchema' = 'true')
                COPY_OPTIONS ('mergeSchema' = 'true')""")
sql.execute(f"""
    INSERT INTO {s.fq('request_log')}
    SELECT e.request_id, e.trace_id, CAST(e.ts AS TIMESTAMP), e.bot_id, e.channel,
           coalesce(e.synthetic, false), e.identity_mode, e.identity_source, e.user_id,
           e.user_hash, e.user_enc, e.user_country, e.user_state, e.user_function,
           e.conversation_id, e.outcome, e.question, e.answer, e.release_id,
           CAST(e.config_version AS INT), e.config_hash, e.manifest_hash, e.model,
           e.platform_version, e.app_version, e.environment, to_json(e.debug.post_rerank),
           e.cited_chunk_ids, to_json(e.conflicts), CAST(e.tokens[0] AS INT), CAST(e.tokens[1] AS INT),
           CAST(e.cost_usd AS DOUBLE), CAST(e.latency_ms AS INT), e.guardrails_fired, e.error,
           CAST(e.ttft_ms AS INT), CAST(e.llm_calls AS INT), CAST(e.retakes AS INT),
           CAST(e.top_score AS DOUBLE), CAST(e.rerank.moved AS INT), length(e.question), e.prompt_uri
    FROM {s.fq('request_events')} e
    LEFT ANTI JOIN {s.fq('request_log')} r ON r.request_id = e.request_id""")
sql.execute(f"""
    INSERT INTO {s.fq('guardrail_events')}
    SELECT ev.event_id, CAST(ev.ts AS TIMESTAMP), e.request_id, e.trace_id, e.bot_id, e.channel,
           e.user_id, e.user_hash, ev.rule, ev.action, ev.reason, ev.content, NULL
    FROM {s.fq('request_events')} e LATERAL VIEW explode(e.events) t AS ev
    LEFT ANTI JOIN {s.fq('guardrail_events')} g ON g.event_id = ev.event_id""")
saved_bots = [b["bot_id"] for b in cp.list_bots()
              if json.loads(b["config_json"]).get("history_mode") == "saved"]
if saved_bots:
    in_list = ", ".join(f":b{i}" for i in range(len(saved_bots)))
    sql.execute(f"""
        INSERT INTO {s.fq('conversations')}
        SELECT conversation_id, bot_id, user_id, 0, role, content, request_id, ts FROM (
          SELECT e.conversation_id, e.bot_id, e.user_id, e.request_id, CAST(e.ts AS TIMESTAMP) AS ts,
                 stack(2, 'user', e.question, 'assistant', e.answer) AS (role, content)
          FROM {s.fq('request_events')} e
          WHERE e.bot_id IN ({in_list}) AND e.user_id IS NOT NULL AND e.conversation_id IS NOT NULL
            AND NOT coalesce(e.synthetic, false)) x
        LEFT ANTI JOIN {s.fq('conversations')} c ON c.request_id = x.request_id AND c.role = x.role""",
        {f"b{i}": b for i, b in enumerate(saved_bots)})
sql.execute(f"CREATE TABLE IF NOT EXISTS {s.fq('health_events')}")
sql.execute(f"""COPY INTO {s.fq('health_events')} FROM '{LOGS}/health'
                FILEFORMAT = JSON FORMAT_OPTIONS ('inferSchema' = 'true', 'mergeSchema' = 'true')
                COPY_OPTIONS ('mergeSchema' = 'true')""")
sql.execute(f"""
    INSERT INTO {s.fq('synthetic_health')}
    SELECT CAST(h.ts AS TIMESTAMP), h.check_name, h.bot_id, h.ok, CAST(h.latency_ms AS INT), h.detail
    FROM {s.fq('health_events')} h
    LEFT ANTI JOIN {s.fq('synthetic_health')} x ON x.ts = CAST(h.ts AS TIMESTAMP) AND x.check_name = h.check_name
      AND x.bot_id <=> h.bot_id""")

# 2. Rollups (OBS-4) and window alerts ------------------------------------------------------
sql.execute(f"""
    MERGE INTO {s.fq('monitoring_rollup')} t USING (
      WITH req AS (
        SELECT window(ts, '15 minutes').start AS window_start, bot_id, latency_ms, outcome, cost_usd,
               guardrails_fired, ttft_ms FROM {s.fq('request_log')}
        WHERE ts >= current_timestamp() - INTERVAL 2 HOURS AND channel = 'live' AND NOT synthetic),
      health AS (
        SELECT window(ts, '15 minutes').start AS window_start, avg(CAST(ok AS INT)) AS availability
        FROM {s.fq('synthetic_health')} WHERE ts >= current_timestamp() - INTERVAL 2 HOURS
          AND check_name = 'endpoint' GROUP BY 1)
      SELECT r.window_start, r.bot_id, count(*) AS requests,
             count_if(outcome IN ('error', 'unavailable')) AS errors,
             count_if(outcome = 'no_source') AS no_source,
             count_if(outcome IN ('blocked', 'refused', 'denied')) AS guardrail_blocks,
             count_if(array_contains(guardrails_fired, 'gateway_guardrail')
                      OR array_contains(guardrails_fired, 'sensitive_output')) AS harmful_blocks,
             CAST(percentile_approx(latency_ms, 0.5) AS INT) AS p50_ms,
             CAST(percentile_approx(latency_ms, 0.95) AS INT) AS p95_ms,
             CAST(percentile_approx(latency_ms, 0.99) AS INT) AS p99_ms,
             sum(cost_usd) AS cost_usd, max(h.availability) AS availability,
             CAST(percentile_approx(ttft_ms, 0.95) AS INT) AS p95_ttft_ms
      FROM req r LEFT JOIN health h ON h.window_start = r.window_start
      GROUP BY r.window_start, r.bot_id) s
    ON t.window_start = s.window_start AND t.bot_id = s.bot_id
    WHEN MATCHED THEN UPDATE SET * WHEN NOT MATCHED THEN INSERT *""")

# Start of the most recent complete 15-minute window
last_window = now.replace(minute=(now.minute // 15) * 15, second=0, microsecond=0) - timedelta(minutes=15)
bots = {b["bot_id"]: b for b in cp.list_bots()}
rows = sql.query(f"SELECT * FROM {s.fq('monitoring_rollup')} WHERE window_start = CAST(:w AS TIMESTAMP)",
                 {"w": last_window.isoformat()})
for row in rows:
    bot = bots.get(row["bot_id"])
    if not bot or bot["state"] != "live":
        continue
    prefs = json.loads(bot["config_json"]).get("alert_prefs", {})
    base = sql.query(f"""SELECT avg(requests) AS b FROM {s.fq('monitoring_rollup')}
                         WHERE bot_id = :b AND window_start >= current_timestamp() - INTERVAL 28 DAYS
                           AND hour(window_start) = hour(CAST(:w AS TIMESTAMP))
                           AND dayofweek(window_start) = dayofweek(CAST(:w AS TIMESTAMP))""",
                     {"b": row["bot_id"], "w": last_window.isoformat()})
    baseline = float(base[0]["b"]) if base and base[0]["b"] else None
    for al in monitoring.evaluate_window(row, mon, prefs, baseline, last_window):
        cp.alert(al.bot_id, al.kind, al.severity, al.message, al.dedupe_key, al.required)

# Platform availability (endpoint probe), independent of traffic
avail = sql.query(f"""SELECT avg(CAST(ok AS INT)) AS a, count(*) AS n FROM {s.fq('synthetic_health')}
                      WHERE check_name = 'endpoint' AND ts >= current_timestamp() - INTERVAL 15 MINUTES""")
if avail and int(avail[0]["n"] or 0) and float(avail[0]["a"]) < mon["availability_target"]:
    cp.alert(None, "availability", "high",
             f"Agent endpoint availability {float(avail[0]['a']):.1%} over 15 minutes.",
             f"availability:platform:{monitoring.window_key(last_window)}")

# 3. Hourly canary (OBS-1) ---------------------------------------------------------------
if now.minute < 15:
    client = llm.openai_client(w)
    for b in bots.values():
        if b["state"] != "live":
            continue
        p = BotPaths(s, b["bot_id"])
        q = sql.query(f"""SELECT question FROM {p.t('golden_set')} WHERE approved AND active
                          AND kind = 'in_scope' ORDER BY difficulty, eval_id LIMIT 1""")
        if not q:
            continue
        started = datetime.now(timezone.utc)
        try:
            _, custom = evals.call_agent(client, s.get("agent.serving_endpoint"), b["bot_id"],
                                         q[0]["question"], channel="live")
            ok, detail = custom.get("outcome") == "answered", custom.get("outcome", "")
        except Exception as e:  # noqa: BLE001
            ok, detail = False, repr(e)[:500]
        ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
        sql.execute(f"INSERT INTO {s.fq('synthetic_health')} VALUES (current_timestamp(), 'canary', :b, "
                    "CAST(:ok AS BOOLEAN), CAST(:ms AS INT), :d)",
                    {"b": b["bot_id"], "ok": ok, "ms": ms, "d": detail})
        if not ok:
            cp.alert(b["bot_id"], "availability", "high", f"Hourly test question failed: {detail}",
                     f"canary:{b['bot_id']}:{now:%Y-%m-%dT%H}")

# 4. Daily: cost, budget, expiry ----------------------------------------------------------------
daily_due = a.force_daily == "true" or (now.hour == 11 and now.minute < 15)  # ~6 AM Central
if daily_due:
    p_price = s.get("pricing")
    sql.execute(f"""
        MERGE INTO {s.fq('cost_daily')} t USING (
          SELECT to_date(ts) AS day, bot_id, sum(cost_usd) AS answer_cost_usd, 0.0 AS judge_cost_usd,
                 0.0 AS parse_cost_usd, sum(cost_usd) AS total_usd, count(*) AS requests
          FROM {s.fq('request_log')} WHERE ts >= date_trunc('MONTH', current_date()) - INTERVAL 1 DAY
          GROUP BY 1, 2) s
        ON t.day = s.day AND t.bot_id = s.bot_id
        WHEN MATCHED THEN UPDATE SET * WHEN NOT MATCHED THEN INSERT *""")
    # Parse cost: pages parsed this month, from each bot's manifest (CST-7)
    today = now.date()
    month = today.strftime("%Y-%m")
    for b in bots.values():
        if b["state"] in ("draft", "deleted"):
            continue
        cfgd = json.loads(b["config_json"])
        p = BotPaths(s, b["bot_id"])
        try:
            pages = sql.query(f"""SELECT coalesce(sum(page_count), 0) AS n FROM {p.t('manifest')}
                                  WHERE uploaded_at >= date_trunc('MONTH', current_date())""")[0]["n"]
        except Exception:  # noqa: BLE001
            pages = 0
        parse_cost = float(pages or 0) * float(p_price.get("parse_per_page", 0))
        mtd = sql.query(f"""SELECT coalesce(sum(answer_cost_usd), 0) AS c FROM {s.fq('cost_daily')}
                            WHERE bot_id = :b AND day >= date_trunc('MONTH', current_date())""",
                        {"b": b["bot_id"]})[0]["c"]
        month_cost = float(mtd or 0) + parse_cost
        budget = float(cfgd.get("budget_usd", 1000))
        for al in monitoring.budget_alerts(b["bot_id"], month_cost, budget,
                                           p_price.get("budget_alert_levels", [0.7, 0.9, 1.0]), month):
            cp.alert(al.bot_id, al.kind, al.severity, al.message, al.dedupe_key, True)
        if monitoring.should_pause_for_budget(month_cost, budget, cfgd.get("budget_action", "continue"),
                                              b["state"]):
            cp.set_state(b["bot_id"], State.BUDGET_PAUSED.value, "system:budget",
                         {"month_cost": month_cost, "budget": budget})
        if today.day == 1 and b["state"] == State.BUDGET_PAUSED.value:
            cp.set_state(b["bot_id"], State.LIVE.value, "system:budget", {"reason": "new month"})
        try:
            docs = sql.query(f"""SELECT doc_id, doc_version, doc_name, expires_at, no_expiry
                                 FROM {p.t('manifest')} WHERE status = 'approved' AND is_active""")
        except Exception:  # noqa: BLE001
            docs = []
        for al in monitoring.expiry_notices(b["bot_id"], docs, int(mon.get("expiry_lead_days", 30)), today):
            cp.alert(al.bot_id, al.kind, al.severity, al.message, al.dedupe_key, True)

print(f"Monitor run complete at {now.isoformat()}")
