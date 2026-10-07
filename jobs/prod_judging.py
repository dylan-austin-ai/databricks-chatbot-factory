"""Daily production quality (MON-4, MON-6, EVL-7, OBS-10).

LLM judging itself runs natively: Databricks production scorers (set up by
setup_observability) score live traces and attach the results to each trace. This job:
1. Copies those trace scores into judge_results for dashboards, drift and alerts.
2. Runs the deterministic citation check on the same sample and attaches it to the trace.
3. Alerts when a bot's 7-day pass rate drops below its threshold, and on guardrail spikes.
4. Sends failed or thumbs-down answers to the bot's MLflow Review Queue (Beta,
   assigned to the owner and Reviewer) and to the golden set as unapproved questions.
"""
import datetime as dt
import time
import uuid

import mlflow
from mlflow.entities import AssessmentSource

from _bootstrap import args, context

from factory import evals
from factory.ingestion import BotPaths
from factory.tracing import assessment_value

a = args("catalog", "lookback_hours", "environment")
spark, sql, settings, cp, w = context(a.catalog)
hours = int(a.lookback_hours or 24)
mlflow.set_tracking_uri("databricks")
exp = mlflow.set_experiment(f"/Shared/chatbot-factory/{a.environment or 'dev'}/traces")
since_ms = int((time.time() - hours * 3600) * 1000)
today = dt.date.today()

# 1. Trace scores -> judge_results ---------------------------------------------------------
done = {(r["request_id"], r["scorer"]) for r in sql.query(
    f"SELECT request_id, scorer FROM {settings.fq('judge_results')} "
    f"WHERE ts >= current_timestamp() - make_interval(0, 0, 0, 0, {hours + 24}, 0, 0)")}
failed: dict[str, list] = {}          # bot_id -> traces needing human review
traces = mlflow.search_traces(experiment_ids=[exp.experiment_id], return_type="list", max_results=50000,
                              filter_string=f"timestamp_ms > {since_ms} AND tags.channel = 'live' "
                                            "AND tags.synthetic = 'false'")
for t in traces:
    tags = t.info.tags or {}
    rid, bot = tags.get("client_request_id"), tags.get("bot_id")
    for x in getattr(t.info, "assessments", None) or []:
        scorer = x.name.removesuffix("_critical").removesuffix("_sampled")
        value = assessment_value(getattr(getattr(x, "feedback", None), "value", getattr(x, "value", None)))
        if value is None or not rid or (rid, scorer) in done:
            continue
        sql.execute(f"INSERT INTO {settings.fq('judge_results')} VALUES "
                    "(:r, :b, :s, :v, :why, current_timestamp())",
                    {"r": rid, "b": bot, "s": scorer, "v": value, "why": (x.rationale or "")[:4000]})
        done.add((rid, scorer))
        if value < 0.5 or (scorer == "user_thumbs" and value == 0):
            failed.setdefault(bot, []).append(t)

# 2. Deterministic citation check on the judged sample, attached to the trace --------------
critical = {b["bot_id"] for b in cp.list_bots() if str(b.get("critical")).lower() == "true"}
rows = sql.query(
    f"""SELECT r.* FROM {settings.fq('request_log')} r
        LEFT ANTI JOIN {settings.fq('judge_results')} j ON j.request_id = r.request_id
                                                       AND j.scorer = 'citation_accuracy'
        WHERE r.ts >= current_timestamp() - make_interval(0, 0, 0, 0, {hours}, 0, 0)
          AND r.outcome = 'answered' AND r.channel = 'live' AND NOT r.synthetic""")
for r in evals.sample_for_judging(rows, critical, float(settings.get("quality.judge_sample_rate", 0.2))):
    ids = list(r.get("cited_chunk_ids") or [])
    texts = [x["chunk_to_retrieve"] for x in sql.query(
        f"SELECT chunk_to_retrieve FROM {BotPaths(settings, r['bot_id']).t('chunked')} WHERE chunk_id IN "
        f"({', '.join(f':c{i}' for i in range(len(ids)))})", {f"c{i}": c for i, c in enumerate(ids)})] if ids else []
    ok, why = evals.citation_accuracy(r["answer"], texts)
    sql.execute(f"INSERT INTO {settings.fq('judge_results')} VALUES "
                "(:r, :b, 'citation_accuracy', :v, :why, current_timestamp())",
                {"r": r["request_id"], "b": r["bot_id"], "v": 1.0 if ok else 0.0, "why": why})
    if r.get("trace_id"):
        try:
            mlflow.log_feedback(trace_id=r["trace_id"], name="citation_accuracy", value=ok, rationale=why,
                                source=AssessmentSource(source_type="CODE", source_id="prod_judging"))
        except Exception as e:  # noqa: BLE001
            print(f"feedback not attached to {r['trace_id']}: {e}")

# 3. Alerts --------------------------------------------------------------------------------
thresholds = {**settings.thresholds(), **settings.get("quality.production_thresholds", {})}
for row in sql.query(f"""SELECT bot_id, scorer, avg(value) AS rate, count(*) AS n
                         FROM {settings.fq('judge_results')}
                         WHERE ts >= current_timestamp() - INTERVAL 7 DAYS
                         GROUP BY bot_id, scorer HAVING count(*) >= 10"""):
    t = thresholds.get(row["scorer"])
    if t and float(row["rate"]) < t:
        cp.alert(row["bot_id"], "quality_drop", "high",
                 f"{row['scorer'].replace('_', ' ')} is {float(row['rate']):.0%} over 7 days (needs {t:.0%}).",
                 dedupe_key=f"quality:{row['bot_id']}:{row['scorer']}:{today}")

for row in sql.query(f"""
    SELECT bot_id,
      avg(CASE WHEN ts >= current_timestamp() - INTERVAL 1 DAY AND outcome IN ('blocked','refused') THEN 1.0
               WHEN ts >= current_timestamp() - INTERVAL 1 DAY THEN 0.0 END) AS today,
      avg(CASE WHEN ts < current_timestamp() - INTERVAL 1 DAY AND outcome IN ('blocked','refused') THEN 1.0
               WHEN ts < current_timestamp() - INTERVAL 1 DAY THEN 0.0 END) AS baseline
    FROM {settings.fq('request_log')} WHERE ts >= current_timestamp() - INTERVAL 8 DAYS
      AND channel = 'live' AND NOT synthetic
    GROUP BY bot_id"""):
    now, base = row["today"], row["baseline"]
    if now is not None and base is not None and float(now) > max(2 * float(base), 0.10):
        cp.alert(row["bot_id"], "guardrail_spike", "medium",
                 f"{float(now):.0%} of requests blocked or refused today vs {float(base):.0%} baseline.",
                 dedupe_key=f"spike:{row['bot_id']}:{today}", required=False)

# 4. Human review (MLflow Review Queues, REV-1) + golden-set feedback loop ----------------
# One queue per bot, assigned to its owner and Reviewer; failed-judge and thumbs-down traces are
# queued for a human verdict. Reviewers work in the experiment's Reviews tab.
try:
    from factory.reviews import queue_traces
    for bot, items in failed.items():
        if bot:
            n = queue_traces(cp.get_config(bot), [t.info.trace_id for t in items])
            print(f"{n} trace(s) queued in review_{bot}")
except Exception as e:  # noqa: BLE001 - Review Queues is a Beta preview an admin turns on
    print(f"Review queue not updated: {e}")

for f in sql.query(f"""SELECT DISTINCT f.request_id, f.bot_id, r.question FROM {settings.fq('feedback')} f
                       JOIN {settings.fq('request_log')} r USING (request_id)
                       JOIN {settings.fq('bots')} b ON b.bot_id = f.bot_id
                       WHERE f.rating = 'down' AND NOT coalesce(f.added_to_eval, false)
                         AND b.state NOT IN ('deleted', 'purged')"""):
    try:
        sql.execute(f"""INSERT INTO {BotPaths(settings, f['bot_id']).t('golden_set')} (eval_id, question,
                        expected_answer, kind, difficulty, question_type, origin, approved, active, updated_by,
                        updated_at) VALUES (:id, :q, '', 'feedback', 'hard', 'user_feedback', 'feedback', false,
                        true, 'system:feedback', current_timestamp())""",
                    {"id": str(uuid.uuid4()), "q": f["question"]})
        sql.execute(f"UPDATE {settings.fq('feedback')} SET added_to_eval = true WHERE request_id = :r",
                    {"r": f["request_id"]})
    except Exception as e:  # noqa: BLE001 - one bad row never blocks the rest
        print(f"feedback {f['request_id']} not added: {e}")
