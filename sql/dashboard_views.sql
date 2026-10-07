-- Views behind the single shared AI/BI dashboard, filtered by bot_id (MON-3, OBS-5),
-- plus redacted views for support staff (PRV-1). Raw tables are restricted to
-- Security/MLOps by the setup job; support staff get only the *_redacted views.
-- Rendered with {catalog}/{platform}/{tag_chatbot}/{shared_value}.

CREATE OR REPLACE VIEW {catalog}.{platform}.v_bot_activity AS
SELECT bot_id, date_trunc('DAY', ts) AS day, count(*) AS requests,
       count_if(outcome = 'answered') AS answered,
       count_if(outcome = 'no_source') AS no_source,
       count_if(outcome IN ('refused', 'blocked')) AS refused_or_blocked,
       count_if(outcome = 'denied') AS denied,
       count_if(outcome IN ('error', 'unavailable')) AS errors,
       count(DISTINCT user_hash) AS users,
       percentile_approx(latency_ms, 0.5) AS p50_latency_ms,
       percentile_approx(latency_ms, 0.95) AS p95_latency_ms,
       percentile_approx(latency_ms, 0.99) AS p99_latency_ms,
       sum(input_tokens) AS input_tokens, sum(output_tokens) AS output_tokens,
       sum(cost_usd) AS cost_usd, sum(cost_usd) / nullif(count(*), 0) AS cost_per_query
FROM {catalog}.{platform}.request_log
WHERE channel = 'live' AND NOT synthetic
GROUP BY ALL;

CREATE OR REPLACE VIEW {catalog}.{platform}.v_monitoring AS
SELECT r.*, CAST(errors AS DOUBLE) / nullif(requests, 0) AS error_rate,
       CAST(no_source AS DOUBLE) / nullif(requests, 0) AS no_source_rate,
       CAST(guardrail_blocks AS DOUBLE) / nullif(requests, 0) AS guardrail_rate
FROM {catalog}.{platform}.monitoring_rollup r;

-- Daily observability per bot (OBS-12): what the app's Monitoring page charts
CREATE OR REPLACE VIEW {catalog}.{platform}.v_observability_daily AS
SELECT bot_id, CAST(date_trunc('DAY', ts) AS DATE) AS day, count(*) AS requests,
       percentile_approx(latency_ms, 0.5) AS p50_latency_ms, percentile_approx(latency_ms, 0.95) AS p95_latency_ms,
       percentile_approx(ttft_ms, 0.5) AS p50_ttft_ms, percentile_approx(ttft_ms, 0.95) AS p95_ttft_ms,
       avg(input_tokens) AS avg_input_tokens, avg(output_tokens) AS avg_output_tokens,
       sum(cost_usd) AS cost_usd, sum(cost_usd) / nullif(count(*), 0) AS cost_per_question,
       avg(CASE WHEN retakes > 0 THEN 1.0 ELSE 0.0 END) AS retake_rate,
       avg(CASE WHEN outcome = 'answered' THEN 1.0 ELSE 0.0 END) AS answered_rate,
       avg(CASE WHEN outcome = 'no_source' THEN 1.0 ELSE 0.0 END) AS no_source_rate,
       avg(CASE WHEN outcome IN ('refused', 'blocked') THEN 1.0 ELSE 0.0 END) AS refused_rate,
       avg(CASE WHEN outcome IN ('error', 'unverified') THEN 1.0 ELSE 0.0 END) AS failed_rate,
       avg(top_score) AS mean_top_score, avg(rerank_moved) AS mean_rerank_moved,
       avg(question_chars) AS mean_question_chars
FROM {catalog}.{platform}.request_log WHERE channel = 'live' AND NOT synthetic GROUP BY ALL;

CREATE OR REPLACE VIEW {catalog}.{platform}.v_quality_daily AS
SELECT bot_id, scorer, date_trunc('DAY', ts) AS day, avg(value) AS pass_rate, count(*) AS judged
FROM {catalog}.{platform}.judge_results GROUP BY ALL;

CREATE OR REPLACE VIEW {catalog}.{platform}.v_eval_latest AS
SELECT bot_id, release_id, channel, ts, passed, needs_safety_review, platform_version,
       git_commit, config_version,
       from_json(metrics_json, 'metrics MAP<STRING, DOUBLE>, zero_failures MAP<STRING, INT>, failures ARRAY<STRING>, n_cases INT') AS m
FROM {catalog}.{platform}.eval_runs
QUALIFY ROW_NUMBER() OVER (PARTITION BY bot_id, channel ORDER BY ts DESC) = 1;

CREATE OR REPLACE VIEW {catalog}.{platform}.v_guardrails AS
SELECT bot_id, date_trunc('DAY', ts) AS day, rule, action, count(*) AS events
FROM {catalog}.{platform}.guardrail_events GROUP BY ALL;

CREATE OR REPLACE VIEW {catalog}.{platform}.v_feedback AS
SELECT bot_id, date_trunc('DAY', ts) AS day, count_if(rating = 'up') AS thumbs_up,
       count_if(rating = 'down') AS thumbs_down
FROM {catalog}.{platform}.feedback GROUP BY ALL;

CREATE OR REPLACE VIEW {catalog}.{platform}.v_alerts_open AS
SELECT * FROM {catalog}.{platform}.alerts WHERE NOT resolved;

CREATE OR REPLACE VIEW {catalog}.{platform}.v_releases AS
SELECT bot_id, release_id, seq, status, eval_passed, smoke_passed, approved_by,
       promoted_at, retired_at, config_hash, manifest_hash, golden_hash, parser_hash,
       platform_version, git_commit, doc_count, chunk_count
FROM {catalog}.{platform}.releases;

CREATE OR REPLACE VIEW {catalog}.{platform}.v_budget AS
SELECT d.bot_id, date_trunc('MONTH', d.day) AS month, sum(d.total_usd) AS variable_cost_usd,
       sum(d.requests) AS requests, sum(d.total_usd) / nullif(sum(d.requests), 0) AS cost_per_query,
       try_cast(get_json_object(b.config_json, '$.budget_usd') AS DOUBLE) AS budget_usd
FROM {catalog}.{platform}.cost_daily d JOIN {catalog}.{platform}.bots b USING (bot_id)
GROUP BY ALL;

-- Fixed shared-platform cost vs bot-unique tagged cost (CST-7)
CREATE OR REPLACE VIEW {catalog}.{platform}.v_cost_by_tag AS
SELECT u.usage_date,
       coalesce(u.custom_tags['{tag_chatbot}'], 'untagged') AS chatbot_name,
       CASE WHEN u.custom_tags['{tag_chatbot}'] = '{shared_value}' THEN 'fixed (shared)'
            ELSE 'bot-unique' END AS cost_type,
       u.custom_tags['team'] AS team, u.custom_tags['function'] AS business_function,
       u.billing_origin_product AS product, sum(u.usage_quantity) AS dbus,
       sum(u.usage_quantity * p.pricing.effective_list.default) AS list_cost_usd
FROM system.billing.usage u
LEFT JOIN system.billing.list_prices p
  ON u.sku_name = p.sku_name AND u.usage_start_time >= p.price_start_time
 AND (p.price_end_time IS NULL OR u.usage_start_time < p.price_end_time)
WHERE u.custom_tags['{tag_chatbot}'] IS NOT NULL
GROUP BY ALL;

-- Question topics, worst first (OBS-20): per bot and topic over the last 28 days
CREATE OR REPLACE VIEW {catalog}.{platform}.v_topics AS
SELECT e.bot_id, e.topic, max(e.topic_label) AS topic_label, count(*) AS questions,
       count_if(e.day >= current_date() - 7) AS last_7_days,
       count_if(e.day < current_date() - 7 AND e.day >= current_date() - 14) AS prior_7_days,
       avg(CASE WHEN r.outcome = 'answered' THEN 1.0 ELSE 0.0 END) AS answered_rate,
       avg(CASE WHEN r.outcome = 'no_source' THEN 1.0 ELSE 0.0 END) AS no_source_rate,
       avg(j.value) AS judge_pass_rate, avg(r.top_score) AS mean_top_score,
       percentile_approx(r.latency_ms, 0.95) AS p95_latency_ms
FROM {catalog}.{platform}.question_embeddings e
JOIN {catalog}.{platform}.request_log r USING (request_id)
LEFT JOIN (SELECT request_id, avg(value) AS value FROM {catalog}.{platform}.judge_results GROUP BY request_id) j
  USING (request_id)
WHERE e.day >= current_date() - 28 AND e.topic IS NOT NULL
GROUP BY e.bot_id, e.topic;

-- Recurring problems (OBS-26, Arize "issues"): judge failures grouped by judge and topic, 28 days
CREATE OR REPLACE VIEW {catalog}.{platform}.v_failure_patterns AS
SELECT j.bot_id, j.scorer, coalesce(e.topic_label, 'Not grouped yet') AS topic, count(*) AS failures,
       count_if(j.ts >= current_date() - 7) AS last_7_days,
       count_if(j.ts < current_date() - 7 AND j.ts >= current_date() - 14) AS prior_7_days,
       max_by(j.rationale, j.ts) AS latest_reason, slice(collect_list(r.trace_id), 1, 5) AS example_traces
FROM {catalog}.{platform}.judge_results j
JOIN {catalog}.{platform}.request_log r USING (request_id)
LEFT JOIN {catalog}.{platform}.question_embeddings e USING (request_id)
WHERE j.value < 1 AND j.ts >= current_date() - 28
GROUP BY j.bot_id, j.scorer, coalesce(e.topic_label, 'Not grouped yet');
