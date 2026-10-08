-- Unity Catalog metric views (OBS-15): one governed definition of each chatbot KPI, used by the
-- Genie agent, dashboards and SQL. Rendered by jobs/setup_observability.py.
-- The source names are rendered with backticks, and a YAML value can't start with a backtick,
-- so each source is double-quoted.
CREATE OR REPLACE VIEW {catalog}.{platform}.mv_chatbot_usage WITH METRICS LANGUAGE YAML AS $$
version: 1.1
source: "{catalog}.{platform}.request_log"
filter: channel = 'live' AND NOT synthetic
fields:
  - name: Day
    expr: DATE_TRUNC('DAY', source.ts)
  - name: Chatbot
    expr: source.bot_id
  - name: Release
    expr: source.release_id
  - name: Outcome
    expr: source.outcome
measures:
  - name: Questions
    expr: COUNT(1)
  - name: Answered rate
    expr: AVG(CASE WHEN source.outcome = 'answered' THEN 1.0 ELSE 0.0 END)
  - name: Not in documents rate
    expr: AVG(CASE WHEN source.outcome = 'no_source' THEN 1.0 ELSE 0.0 END)
  - name: Cost USD
    expr: SUM(source.cost_usd)
  - name: Cost per question USD
    expr: SUM(source.cost_usd) / COUNT(1)
  - name: P95 answer time ms
    expr: PERCENTILE_APPROX(source.latency_ms, 0.95)
  - name: P95 time to first token ms
    expr: PERCENTILE_APPROX(source.ttft_ms, 0.95)
  - name: Guardrail retake rate
    expr: AVG(CASE WHEN source.retakes > 0 THEN 1.0 ELSE 0.0 END)
  - name: Mean best search score
    expr: AVG(source.top_score)
$$;

CREATE OR REPLACE VIEW {catalog}.{platform}.mv_chatbot_quality WITH METRICS LANGUAGE YAML AS $$
version: 1.1
source: "{catalog}.{platform}.judge_results"
fields:
  - name: Day
    expr: DATE_TRUNC('DAY', source.ts)
  - name: Chatbot
    expr: source.bot_id
  - name: Judge
    expr: source.scorer
measures:
  - name: Pass rate
    expr: AVG(source.value)
  - name: Answers judged
    expr: COUNT(1)
$$;
