"""Daily drift (OBS-12): are people asking different things than at launch?

1. Embed yesterday's live questions with the same embedding model as the index
   (ai_query, capped per bot per day).
2. Per bot: cosine distance of yesterday's question centroid to the launch baseline (first 14
   days live) and to the prior 7 days, plus answer-side signals, into question_drift. A data
   quality monitor on question_drift (setup_observability) adds statistical drift tests.
3. Refresh the 2-D question map (PCA over the last 28 days) and question topics (k-means; each
   topic gets a short, people-safe name written from its most typical questions) for the Monitoring
   and All chatbots pages.
"""
import numpy as np
from _bootstrap import args, context

from factory.drift import kmeans, pca_2d, topic_count
from factory.sql import ident

a = args("catalog")
spark, sql, settings, cp, w = context(a.catalog)
s = settings
emb, drift, log = s.fq("question_embeddings"), s.fq("question_drift"), s.fq("request_log")
model = s.get("models.embedding_endpoint")
cap = int(s.get("monitoring.embed_daily_cap", 2000))

# 1. Embed (one batch SQL statement; ai_query runs on the shared embedding endpoint)
sql.execute(f"""
    INSERT INTO {emb}
    SELECT request_id, bot_id, day, outcome, ai_query('{model}', question), NULL, NULL, NULL, NULL FROM (
      SELECT r.request_id, r.bot_id, CAST(r.ts AS DATE) AS day, r.outcome, r.question,
             row_number() OVER (PARTITION BY r.bot_id ORDER BY rand()) AS n
      FROM {log} r LEFT ANTI JOIN {emb} e ON e.request_id = r.request_id
      WHERE r.ts >= current_date() - INTERVAL 1 DAY AND r.ts < current_date()
        AND r.channel = 'live' AND NOT r.synthetic AND length(trim(r.question)) > 0)
    WHERE n <= {cap}""")

# 2. Centroid drift for yesterday, computed in SQL over exploded vectors (bounded window)
sql.execute(f"""
    INSERT INTO {drift}
    WITH first_day AS (SELECT bot_id, min(day) AS d0 FROM {emb} GROUP BY bot_id),
    v AS (
      SELECT e.bot_id, pos, val,
             CASE WHEN e.day = current_date() - 1 THEN 'today'
                  WHEN e.day BETWEEN current_date() - 8 AND current_date() - 2 THEN 'week' END AS w,
             e.day BETWEEN f.d0 AND f.d0 + 13 AS launch
      FROM {emb} e JOIN first_day f USING (bot_id)
      LATERAL VIEW posexplode(e.embedding) x AS pos, val
      WHERE e.day >= current_date() - 8 OR e.day BETWEEN f.d0 AND f.d0 + 13),
    c AS (
      SELECT bot_id, pos, avg(CASE WHEN w = 'today' THEN val END) AS t,
             avg(CASE WHEN w = 'week' THEN val END) AS k, avg(CASE WHEN launch THEN val END) AS l
      FROM v GROUP BY bot_id, pos),
    d AS (
      SELECT bot_id,
             1 - sum(t * l) / (sqrt(sum(t * t)) * sqrt(sum(l * l))) AS drift_vs_launch,
             1 - sum(t * k) / (sqrt(sum(t * t)) * sqrt(sum(k * k))) AS drift_vs_last_week
      FROM c GROUP BY bot_id),
    o AS (
      SELECT bot_id, count(*) AS questions, avg(CASE WHEN outcome = 'no_source' THEN 1.0 ELSE 0.0 END) AS nsr,
             avg(top_score) AS mts, CAST(percentile_approx(ttft_ms, 0.95) AS INT) AS ttft,
             sum(cost_usd) / nullif(count(*), 0) AS cpq
      FROM {log} WHERE ts >= current_date() - INTERVAL 1 DAY AND ts < current_date()
        AND channel = 'live' AND NOT synthetic GROUP BY bot_id)
    SELECT current_date() - 1, o.bot_id, o.questions, d.drift_vs_launch, d.drift_vs_last_week,
           o.nsr, o.mts, o.ttft, o.cpq
    FROM o LEFT JOIN d USING (bot_id)
    WHERE NOT EXISTS (SELECT 1 FROM {drift} x WHERE x.day = current_date() - 1 AND x.bot_id = o.bot_id)""")



def topic_name(j: int, questions: list[str]) -> str:
    """A short, people-safe topic name from a topic's most typical questions (never raw question text)."""
    prompt = ("Name the common topic of these questions in 2 to 6 plain words. Do not include names, numbers, "
              "emails or any personal details. Reply with the topic name only.\n- " + "\n- ".join(q[:300] for q in questions))
    try:
        out = sql.query(f"SELECT ai_query('{s.get('models.generation_endpoint')}', :p) AS t", {"p": prompt})[0]["t"]
        return str(out).strip().strip('"').splitlines()[0][:60] or f"Topic {j + 1}"
    except Exception:  # noqa: BLE001
        return f"Topic {j + 1}"


# 3. 2-D question map and topics (Arize-style embedding and cluster views), per bot, last 28 days
for (bot,) in spark.sql(f"SELECT DISTINCT bot_id FROM {emb} WHERE day >= current_date() - 28").collect():
    ident(bot)  # validated technical name before it goes into SQL
    rows = spark.sql(f"""SELECT e.request_id, e.embedding, r.question FROM {emb} e JOIN {log} r USING (request_id)
                         WHERE e.bot_id = '{bot}' AND e.day >= current_date() - 28 LIMIT 5000""").collect()
    if len(rows) < 3:
        continue
    x = np.array([r.embedding for r in rows], dtype=np.float32)
    xy = pca_2d(x)
    labels, cents = kmeans(x, topic_count(len(rows)))
    xn = x / np.maximum(np.linalg.norm(x, axis=1, keepdims=True), 1e-9)
    name = {j: topic_name(j, [rows[i].question for i in np.argsort(-np.where(labels == j, xn @ cents[j], -2))[:5]])
            for j in set(labels)}
    spark.createDataFrame([(r.request_id, float(p[0]), float(p[1]), int(t), name[t]) for r, p, t in zip(rows, xy, labels)],
                          "request_id STRING, x DOUBLE, y DOUBLE, topic INT, topic_label STRING"
                          ).createOrReplaceTempView("xy")
    spark.sql(f"MERGE INTO {emb} t USING xy ON t.request_id = xy.request_id WHEN MATCHED THEN UPDATE SET "
              "t.x = xy.x, t.y = xy.y, t.topic = xy.topic, t.topic_label = xy.topic_label")

# Alert when today's questions moved far from launch (owner may need new documents)
limit = float(s.get("monitoring.question_drift_alert", 0.15))
for r in sql.query(f"SELECT * FROM {drift} WHERE day = current_date() - 1 AND drift_vs_launch > {limit}"):
    cp.alert(r["bot_id"], "question_drift", "medium",
             f"Questions have shifted away from launch (drift {float(r['drift_vs_launch']):.2f}). "
             "People may be asking about topics your documents don't cover.",
             dedupe_key=f"qdrift:{r['bot_id']}:{r['day']}", required=False)
