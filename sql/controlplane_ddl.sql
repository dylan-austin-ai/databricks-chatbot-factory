-- Control plane (LCY-2). Rendered by factory.controlplane with {catalog}/{platform}.
-- Every statement is idempotent so the setup job can be re-run safely.

CREATE SCHEMA IF NOT EXISTS {catalog}.{platform}
  COMMENT 'Chatbot Factory control plane. Managed by the app; no manual edits (LCY-5, REL-10).';

ALTER SCHEMA {catalog}.{platform} SET TAGS ('{tag_chatbot}' = '{shared_value}');

-- Runtime files the agent reads without a SQL warehouse (bot configs, settings),
-- plus agent request logs and health probes written as JSON (PRV-3, OBS-1).
CREATE VOLUME IF NOT EXISTS {catalog}.{platform}.runtime
  COMMENT 'Published bot configs and settings read by the agent';
CREATE VOLUME IF NOT EXISTS {catalog}.{platform}.logs
  COMMENT 'RESTRICTED: raw request/debug logs and health probes (PRV-1, PRV-3)';

-- Admin overrides of platform_defaults.yml (ADM-2)
CREATE TABLE IF NOT EXISTS {catalog}.{platform}.platform_settings (
  key STRING NOT NULL, value_json STRING, updated_by STRING, updated_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS {catalog}.{platform}.bots (
  bot_id STRING NOT NULL,          -- schema name, immutable
  display_name STRING,
  config_json STRING,              -- latest BotConfig
  config_version INT,
  state STRING,                    -- see factory.lifecycle.State
  owner_user STRING, owner_group STRING, reviewer STRING,
  access_mode STRING, sensitivity STRING,
  critical BOOLEAN,                -- admin only (ADM-3)
  live_release_id STRING,          -- REL-1/2
  candidate_release_id STRING,
  platform_pin STRING, pin_expires_at TIMESTAMP,   -- CAS-6
  created_by STRING, created_at TIMESTAMP, updated_at TIMESTAMP,
  deleted_at TIMESTAMP,            -- soft delete (LCY-6)
  last_request_at TIMESTAMP
) TBLPROPERTIES (delta.enableChangeDataFeed = true);

CREATE TABLE IF NOT EXISTS {catalog}.{platform}.bot_versions (
  bot_id STRING, version INT, config_json STRING, changed_by STRING,
  changed_at TIMESTAMP, reason STRING
);

-- Releases (REL-1..9). One open candidate per bot; promoted releases become live.
CREATE TABLE IF NOT EXISTS {catalog}.{platform}.releases (
  release_id STRING NOT NULL, bot_id STRING, seq INT,
  status STRING,                   -- candidate | live | superseded | rolled_back | discarded
  config_version INT, config_hash STRING, manifest_hash STRING,
  golden_hash STRING, parser_hash STRING,
  platform_version STRING, git_commit STRING, environment STRING,
  doc_count INT, chunk_count INT,
  eval_run_id STRING, eval_passed BOOLEAN,
  safety_override_by STRING, safety_override_reason STRING,   -- EVG-3
  submitted_by STRING, review_requested_at TIMESTAMP, approved_by STRING,
  created_at TIMESTAMP, updated_at TIMESTAMP, promoted_at TIMESTAMP, retired_at TIMESTAMP,
  smoke_passed BOOLEAN, notes STRING,
  model_id STRING                  -- MLflow LoggedModel for this release (OBS-13)
);

-- Immutable hashed snapshots per release (REL-9)
CREATE TABLE IF NOT EXISTS {catalog}.{platform}.snapshots (
  release_id STRING, bot_id STRING, kind STRING,   -- config | manifest | golden | parser
  content_hash STRING, content_json STRING, created_at TIMESTAMP
);

-- Membership of each release: which chunks, with which metadata (REL-3 rollback)
CREATE TABLE IF NOT EXISTS {catalog}.{platform}.release_chunks (
  release_id STRING, bot_id STRING, chunk_id STRING, doc_id STRING, doc_version INT,
  effective_ts BIGINT, expires_ts BIGINT, security_scope STRING, geo_scope STRING
);

-- When each chunk was live, so the index can be reconstructed for any date (REL-4).
-- Live on day D: live_from <= D < coalesce(live_to, inf) AND effective_ts <= D < expires_ts
CREATE TABLE IF NOT EXISTS {catalog}.{platform}.index_history (
  bot_id STRING, chunk_id STRING, doc_id STRING, doc_version INT, release_id STRING,
  live_from TIMESTAMP, live_to TIMESTAMP, effective_ts BIGINT, expires_ts BIGINT,
  security_scope STRING, geo_scope STRING
);

-- Idempotent provisioning state (PRV-6)
CREATE TABLE IF NOT EXISTS {catalog}.{platform}.provisioning_steps (
  bot_id STRING, step STRING, status STRING, detail STRING, updated_at TIMESTAMP
);

-- Every lifecycle and document action (LCY-7, DOC-13)
CREATE TABLE IF NOT EXISTS {catalog}.{platform}.audit_log (
  event_id STRING, ts TIMESTAMP, actor STRING, bot_id STRING,
  action STRING, target STRING, detail_json STRING
);

-- Shared AI Search index source (ARC-4). One row per chunk; in_live / in_candidate
-- decide which channel can retrieve it (REL-1); expiry is filtered at query time (DCL-1).
CREATE TABLE IF NOT EXISTS {catalog}.{platform}.shared_chunks (
  chunk_id STRING NOT NULL, bot_id STRING, doc_id STRING, doc_version INT,
  doc_name STRING, source_uri STRING, page_ids ARRAY<INT>, section STRING,
  chunk_to_retrieve STRING, chunk_to_embed STRING,
  doc_type STRING, effective_date STRING, department STRING,
  in_live BOOLEAN, in_candidate BOOLEAN,
  effective_ts BIGINT, expires_ts BIGINT,          -- epoch seconds; no-expiry = 4102444800
  security_scope STRING, geo_scope STRING,         -- doc-level access, day 2 (IDN-4)
  updated_at TIMESTAMP
) TBLPROPERTIES (delta.enableChangeDataFeed = true);

-- User reference data (IDN-3): location, function, scopes, validity
CREATE TABLE IF NOT EXISTS {catalog}.{platform}.user_access (
  user_id STRING NOT NULL, country STRING, state STRING, business_function STRING,
  security_scopes ARRAY<STRING>, active BOOLEAN, valid_from DATE, valid_to DATE,
  source STRING, updated_at TIMESTAMP
);

-- Saved conversation history (CNV-1), only for bots set to "saved per user"
CREATE TABLE IF NOT EXISTS {catalog}.{platform}.conversations (
  conversation_id STRING, bot_id STRING, user_id STRING, turn INT,
  role STRING, content STRING, request_id STRING, ts TIMESTAMP
);

-- RESTRICTED raw request log (PRV-1), loaded from the logs volume by the monitor job
CREATE TABLE IF NOT EXISTS {catalog}.{platform}.request_log (
  request_id STRING, trace_id STRING, ts TIMESTAMP, bot_id STRING,
  channel STRING,                          -- live | candidate (REL-6)
  synthetic BOOLEAN,                       -- probe / eval traffic, excluded from metrics
  identity_mode STRING,                    -- named | pseudonymous (IDN-2)
  identity_source STRING,                  -- obo | middleware
  user_id STRING,                          -- NULL for pseudonymous bots
  user_hash STRING, user_enc STRING,       -- keyed hash; encrypted identity (break-glass)
  user_country STRING, user_state STRING, user_function STRING,
  conversation_id STRING,
  outcome STRING,  -- answered | no_source | refused | blocked | denied | unverified | unavailable | error
  question STRING, answer STRING,
  release_id STRING, config_version INT, config_hash STRING, manifest_hash STRING,
  model STRING, platform_version STRING, app_version STRING, environment STRING,
  retrieval_json STRING,                   -- ranked post-rerank results (RET-1, ANS-3)
  cited_chunk_ids ARRAY<STRING>, conflicts_json STRING,
  input_tokens INT, output_tokens INT, cost_usd DOUBLE, latency_ms INT,
  guardrails_fired ARRAY<STRING>, error STRING,
  -- Observability metrics (OBS-7, OBS-12): also on the MLflow trace as tags/span attributes
  ttft_ms INT,                             -- time to first token from the answer model
  llm_calls INT, retakes INT,              -- model calls; answers regenerated by guardrails
  top_score DOUBLE,                        -- best retrieval score (retrieval drift)
  rerank_moved INT,                        -- results the reranker reordered
  question_chars INT, prompt_uri STRING    -- input drift; Prompt Registry version
);

-- RESTRICTED guardrail events (PRV-2)
CREATE TABLE IF NOT EXISTS {catalog}.{platform}.guardrail_events (
  event_id STRING, ts TIMESTAMP, request_id STRING, trace_id STRING, bot_id STRING,
  channel STRING, user_id STRING, user_hash STRING, rule STRING, action STRING,
  reason STRING, content STRING, detail_json STRING
);

CREATE TABLE IF NOT EXISTS {catalog}.{platform}.eval_runs (
  run_id STRING, bot_id STRING, release_id STRING, channel STRING, trigger STRING,
  platform_version STRING, git_commit STRING, config_version INT, metrics_json STRING,
  passed BOOLEAN, needs_safety_review BOOLEAN, ts TIMESTAMP
);

-- Platform (agent/app code) deployments: exact commit per environment (REL-7)
-- One row per agent deployment: what source was deployed and with which configuration (REL-7,
-- REL-8). Columns added after the first release are also listed in ControlPlane.MIGRATIONS.
CREATE TABLE IF NOT EXISTS {catalog}.{platform}.platform_releases (
  release_id STRING, platform_version STRING, git_commit STRING, environment STRING,
  model_version STRING, gate_enforced BOOLEAN, deployed_by STRING, ts TIMESTAMP,
  git_branch STRING, git_origin STRING,
  config_json STRING      /* catalog, workspace, endpoint, warehouse, usage policy, settings hash */
);

CREATE TABLE IF NOT EXISTS {catalog}.{platform}.strategy_results (
  run_id STRING, bot_id STRING, strategy STRING, metrics_json STRING,
  chosen BOOLEAN, explanation STRING, ts TIMESTAMP
);

-- Sampled production judging (MON-6) and user feedback (EVL-7)
CREATE TABLE IF NOT EXISTS {catalog}.{platform}.judge_results (
  request_id STRING, bot_id STRING, scorer STRING, value DOUBLE,
  rationale STRING, ts TIMESTAMP
);

CREATE TABLE IF NOT EXISTS {catalog}.{platform}.feedback (
  request_id STRING, bot_id STRING, user_id STRING, rating STRING,
  comment STRING, ts TIMESTAMP, added_to_eval BOOLEAN
);

-- Synthetic health (OBS-1), loaded from probe files in the logs volume
CREATE TABLE IF NOT EXISTS {catalog}.{platform}.synthetic_health (
  ts TIMESTAMP, check_name STRING, bot_id STRING, ok BOOLEAN, latency_ms INT, detail STRING
);

-- 15-minute rollups (OBS-4)
CREATE TABLE IF NOT EXISTS {catalog}.{platform}.monitoring_rollup (
  window_start TIMESTAMP, bot_id STRING, requests INT, errors INT,
  no_source INT, guardrail_blocks INT, harmful_blocks INT,
  p50_ms INT, p95_ms INT, p99_ms INT, cost_usd DOUBLE, availability DOUBLE,
  p95_ttft_ms INT
);

-- Answer-prompt optimization history (PRM-2)
CREATE TABLE IF NOT EXISTS {catalog}.{platform}.prompt_optimizations (
  ts TIMESTAMP, prompt_name STRING, from_version STRING, to_version STRING,
  initial_score DOUBLE, final_score DOUBLE, status STRING, actor STRING
);

-- Question embedding drift (OBS-12): live questions embedded daily, 2-D map for the app
CREATE TABLE IF NOT EXISTS {catalog}.{platform}.question_embeddings (
  request_id STRING, bot_id STRING, day DATE, outcome STRING, embedding ARRAY<FLOAT>,
  x DOUBLE, y DOUBLE,                      -- PCA projection, refreshed daily
  topic INT, topic_label STRING            -- k-means topic and its short, people-safe name (OBS-20)
);
CREATE TABLE IF NOT EXISTS {catalog}.{platform}.question_drift (
  day DATE, bot_id STRING, questions INT,
  drift_vs_launch DOUBLE,                  -- cosine distance of the day's question centroid
  drift_vs_last_week DOUBLE,               --   to the first 14 days live / the prior 7 days
  no_source_rate DOUBLE, mean_top_score DOUBLE, p95_ttft_ms INT, cost_per_question DOUBLE
);

-- Variable cost per bot per day (CST-7)
CREATE TABLE IF NOT EXISTS {catalog}.{platform}.cost_daily (
  day DATE, bot_id STRING, answer_cost_usd DOUBLE, judge_cost_usd DOUBLE,
  parse_cost_usd DOUBLE, total_usd DOUBLE, requests INT
);

-- Owner and MLOps alerts (MON-4, OBS-5); emailed by per-bot SQL alerts
CREATE TABLE IF NOT EXISTS {catalog}.{platform}.alerts (
  alert_id STRING, ts TIMESTAMP, bot_id STRING, kind STRING,
  severity STRING, message STRING, resolved BOOLEAN, dedupe_key STRING, required BOOLEAN
);
