# Architecture notes

Requirement IDs refer to the "Chatbot Builder App — Requirements" doc.

## Principles

- **Smallest working path.** One agent, one guardrailed endpoint, one AI Search endpoint,
  shared index. Pure logic lives in small modules with tests; jobs are thin scripts.
- **No warehouse in the hot path.** The agent reads bot config and settings from JSON in
  `<catalog>._platform.runtime` (written by the control plane on every change) and writes one
  JSON log per request to `<catalog>._platform.logs`. The monitor job loads logs with COPY INTO.
- **Unity Catalog decides access.** OBO callers are checked with a `SELECT` on the bot's
  `access_probe` (live) or `tester_probe` (test version). Trusted middleware service principals
  may assert `end_user`; anyone else asserting it is ignored.

## Releases (REL-*)

Index rows carry `in_live` and `in_candidate`. `Releases.stage` MERGEs the approved set into
the index source touching only changed rows, records `release_chunks`, snapshots and hashes.
`promote` flips flags (no re-embedding), `rollback` restores from `release_chunks`.
`index_history` is SCD2 so the index can be rebuilt for any day. Expiry and effective dates are
enforced at query time by filters, so nothing needs re-indexing when a document expires.

## Identity (IDN-*)

Named bots log the user. Pseudonymous bots log an HMAC pseudonym plus a Fernet-encrypted
identity; only Security holds the keys and can run `identity.decrypt_identity` (break-glass).

## Quality (EVL-*, EVG-*)

Golden sets mix easy questions and hard extraction questions (tables, footnotes, multi-page)
with expected pages. `compare_strategies` scores 5 retrieval strategies on Hit@k/MRR without
LLM calls and picks the best (ties → simpler). `run_evals` applies the combined gate.
Production judging samples 20% (100% for critical bots).

## Monitoring (OBS-*, CST-*, DCL-*)

App thread probes endpoint and index every 60 s (no compute). Hourly synthetic canary.
15-minute rollups drive latency (5/12/20 s), error rate (5%), availability, harmful output,
and optional traffic alerts within 8 AM ET – 6 PM PT. Budgets are computed from logged tokens
(shared endpoints can't be split by billing tags); alerts at 70/90/100%, owner picks pause or
continue. Expiry notices 30 days ahead. Alerts are rows in `alerts`, emailed by SQL alerts.

## Observability (OBS-6..12)

Every question is one MLflow trace, stored in Unity Catalog (`_platform.traces_otel_*`, OTel
format) behind the experiment `/Shared/chatbot-factory/<env>/traces`, so it is viewable in the
MLflow Traces UI and queryable with SQL. Raw payloads are also captured by AI Gateway inference
tables on the agent endpoint and each answer endpoint; the JSON log and `request_log` remain the
record of truth for dashboards and alerts.

| Layer | What you see | Where |
|---|---|---|
| Spans | config, identity, access, history, rewrite, retrieval (vector + rerank as documents), injection strip, prompt, each answer attempt and retake, output guardrail check, gateway block, redaction, render | MLflow trace view (`factory/tracing.py` lists the tree) |
| Model calls | native token usage (OpenAI autolog), TTFT (streamed), latency, cost, model, attempt | span attributes `llm.*` |
| Trace tags | bot, channel, release, outcome, cost, TTFT, tokens, retakes, top score, guardrails fired, prompt version, critical, synthetic | filter/search in MLflow; `request_log` columns |
| Sessions and users | conversations grouped; user or pseudonym hash | `mlflow.trace.session` / `mlflow.trace.user` |
| Scores on traces | built-in judges (Claude Haiku 4.5: groundedness, retrieval relevance and sufficiency, relevance, safety, platform rules; conversation judges optional) at 20% / 100% critical, switchable per bot; citation check; user thumbs; every eval score | trace assessments; copied to `judge_results` |
| Review | failed or thumbs-down answers queued for the owner and Reviewer | MLflow Review Queue `review_<bot>` (Beta) |
| Prompts | answer prompt versioned per deploy, stamped on traces | Prompt Registry `<catalog>._platform.answer_prompt` |
| Evals | each eval run linked to its questions' traces; releases compare question by question | MLflow runs in the traces experiment |
| Drift | statistical drift per field, slice and release (PSI, KS, JS, Wasserstein, chi-squared) plus auto dashboard; question-embedding drift vs launch and last week; 2-D question map | Data quality monitors on `request_log` and `question_drift`; app Monitoring page |
| Alerts | latency, TTFT, errors, availability, harmful output, quality drop, guardrail spike, question drift, budget, expiry | `alerts` table, emailed by SQL alerts |

Model calls stream from the answer endpoint to measure time to first token; the user still
receives only the verified answer. If an endpoint can't stream, the agent falls back to a normal
call for the rest of its life and logs why.

## Unity Gateway (GW-1..7)

Answer models are model services (UC securables). `deploy_agent` creates them and applies routing
and ordered fallbacks (`factory/gateway.py`); rate limits, inference tables, EXECUTE grants and
service policies are set in the Unity Gateway UI (docs/SETUP.md, Part C). The agent calls
`<host>/ai-gateway/mlflow/v1` with `model=<catalog>._platform.answer_haiku` and a
`Databricks-Ai-Gateway-Request-Tags` header (bot, release, channel, request, conversation), so
`system.ai_gateway.usage` attributes every token to a bot. Guardrails are service policies
attached in the UI; a DENY arrives as HTTP 200 with `databricks_service_policy`, which the agent
records as a block (request_log outcome, guardrail_events, a `guardrail.gateway` span) while the
person sees a generic message. Log-mode policies run on our `guardrail_evaluator` model service,
whose inference table is exposed as `v_guardrail_verdicts` (joined to requests through the
`request_id` tag), so non-blocking verdicts are visible too. A platform-wide spend cap is an
account budget with block-at-100%, created in the account console.

## Quality (JDG-1..4, REV-1, PRM-1/2)

`run_evals` runs each approved question through the deployed agent inside
`mlflow.genai.evaluate`: Databricks built-in judges (Claude Haiku 4.5) plus code scorers for
retrieval Hit@k/MRR, citations, leakage and sensitive output; the agent's retrieval is mirrored
into a RETRIEVER span so retrieval judges can read it. Results attach to traces, the run links
to the release's LoggedModel, and the golden set is mirrored into a UC evaluation dataset.
Judges are switchable per bot (custom `make_judge`, simulated conversations and multi-turn
judges off by default). Production judges run natively on 20% of live traces (100% for
critical bots), gated per bot by trace tags. Failed or thumbs-down answers go to a Review Queue
for the owner and Reviewer. The answer prompt is served from the Prompt Registry `@production`
alias; `optimize_prompt` (GEPA) proposes `@optimized`, which MLOps can promote.

## Governance and cost (GOV-10..12, CST-9..12)

ABAC column masks (`ai_mask` for text, hashes for identities) on tagged columns replace the
redacted views; Data Classification tags PII across the catalog; anomaly detection watches the
control plane's freshness. Warehouse statements carry query tags; jobs, the app and the agent
endpoint carry a serverless usage policy; cost views reconcile our token estimate with gateway
usage and billing.

## Hosting: why Model Serving, not Agent Runtime (yet)

Agent Runtime (Agent Bricks CLI, Beta) adds durable sessions and crash recovery but uses a
different request API (`/api/invocations`, no `custom_inputs`), OAuth-only callers, no gateway
inference logging in front of the agent, and no Review App chat. Model Serving is labeled Legacy
but has no end-of-support date. The answer logic is framework-neutral, so a second thin wrapper
can be added when Agent Runtime is GA.

## Data model

Control plane `_platform`: settings, bots, bot_versions, releases, release_chunks, snapshots,
index_history, provisioning_steps, audit_log, shared_chunks, user_access, conversations,
request_log, guardrail_events, question_embeddings, question_drift, traces_otel_* (MLflow), eval_runs, strategy_results, judge_results, feedback,
synthetic_health, monitoring_rollup, cost_daily, alerts, platform_releases; `v_*` views and
`*_redacted` views for Support.

Per bot: volume `docs`, `manifest`, `parsed_elements`, `chunked`, `index_source`
(Confidential), `golden_set`, `access_probe`, `tester_probe`.

Retention: 7 years (`jobs/maintenance.py`); `audit_log` is never purged.

## Arize parity (October 7)

Arize features that MLflow on Databricks doesn't provide out of the box, built from MLflow traces
and our Delta tables (`factory/flow.py`, `jobs/drift.py`, `sql/dashboard_views.sql`):

| Arize view | Ours | Data |
|---|---|---|
| Agent graph / trace flow | Traces › Typical flow (tree, p50/p95, runs, cost, code per step) | MLflow traces (`search_traces`) |
| Span latency distributions | Histogram per step; All chatbots › time per step | span start/end |
| Embedding cluster analyzer | Question topics worst first (k-means daily over 28 days, each given a short people-safe name by the LLM) | `question_embeddings.topic`, `v_topics` |
| Performance slices / pivots | All chatbots › Compare slices, with counts | `request_log` + `judge_results` |
| Retrieval analysis | Gaps in the documents (best-score distribution, lowest-scoring unanswered questions) | `request_log.top_score` |
| Sessions | Conversations (questions per conversation, asked again after a miss, left after a miss) | `request_log.conversation_id` |
| Latency heatmap | Slow answers by weekday and hour | `request_log` |

Already covered natively: UC trace storage, built-in judges in dev and production, review queues,
data quality drift, Genie over the tables, prompt registry and optimization. Not built: an Alyx-style
copilot (Genie covers questions over the tables) and automatic issue grouping beyond MLflow's own
issue detection (run from the MLflow UI; listed on the setup checklist).
