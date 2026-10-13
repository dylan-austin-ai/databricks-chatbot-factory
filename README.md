# Chatbot Factory

A Databricks App that lets non-technical people build governed RAG chatbots from their own
documents through a guided wizard. Everything is Databricks-native, runs on shared
infrastructure, and is governed by Unity Catalog.

```
Owner ─► App (wizard · documents · test questions · launch · chat · admin)
           │ Go
           ▼
   provision → ingest jobs (serverless)
     ai_parse_document → ai_prep_search → chunked → QA, injection scan, PII block
     golden set (easy + hard extraction questions) → strategy comparison → eval gate
           │ stage = candidate release (in_candidate rows in the index)
           ▼
   AI Search: one endpoint · shared index (bot_id filter) · dedicated index for Confidential
           ▲  filters: bot_id · channel · effective_ts ≤ now < expires_ts · rerank
User / middleware ─► ONE agent endpoint (config from runtime volume, no warehouse)
           │ identity (OBO, or trusted middleware asserting end_user) · UC access probe
           │ Haiku via guardrailed AI Gateway endpoint · JSON answer · excerpt verification
           ▼
   MLflow trace per question (spans, tokens, TTFT, cost, judge scores) → UC trace tables
   JSON logs → logs volume → monitor job (15 min) → request_log, guardrail_events, rollups,
   alerts (email via SQL alerts), budgets, expiry notices · hourly canary · app health probe
```

**Releases.** Bots have a *test* (candidate) and *live* version in prod; no dev copy per bot.
Changes stage a candidate, the combined gate runs (Hit@1 ≥ .80, Hit@3 ≥ .90, Hit@5 ≥ .95,
MRR ≥ .85, judges ≥ 90%, zero deterministic failures; unsafe flags go to the Reviewer), the
Reviewer approves, the promote job flips rows to live, syncs, smoke-tests and auto-rolls back on
failure. `releases`, `release_chunks` and `index_history` reconstruct the index for any day
(`Releases.index_as_of_sql`). Platform code moves QA → prod as the exact git commit.

## Layout

| Path | What it is |
|---|---|
| `src/factory/` | Library: config, ingestion SQL, releases, provisioning, guardrails, identity, evals, strategies, monitoring, alerting, `journey` (what an owner should do next), `problems` (errors and alerts in plain words) |
| `src/factory/platform_defaults.yml` | Defaults; MLOps edits them on the Admin page (stored in `platform_settings`, published to the runtime volume) |
| `agent/agent.py`, `agent/deploy.py` | The single shared agent and its gated deploy |
| `app/` | Streamlit app styled to the Claude Design screens (`app/ui.py`, `.streamlit/config.toml`) |
| `docs/design/` | The design canvas files (17 screens) |
| `jobs/` | setup_platform, setup_observability, provision, ingest (+ per-bot file-arrival triggers), compare_strategies, run_evals (`mlflow.genai.evaluate`), promote, monitor, prod_judging, drift, optimize_prompt, reindex, git_sync, maintenance |
| `sql/` | Control-plane DDL, per-bot DDL, dashboard views, ABAC governance, metric views, system-table cost views |
| `resources/`, `databricks.yml` | Declarative Automation Bundle (qa and prod targets, direct deployment engine) |
| `tests/` | 290 unit tests for the pure logic, and 79 that run every page of the app against a pretend workspace (these need the `app` extra) |

## Prerequisites

**docs/SETUP.md is the step-by-step guide** (every UI click, command and setting, in order).
Summary:

1. AWS us-east workspace with Unity Catalog, serverless, Model Serving, AI Search, Apps, and
   `ai_parse_document` / `ai_prep_search` (Beta: enable on Previews; DBR 18.2+).
2. Serverless SQL warehouse.
3. **Unity Gateway** (GA). `deploy_agent` creates the model services
   `<catalog>._platform.answer_haiku` (Haiku 4.5, Sonnet 4.5 fallback) and
   `guardrail_evaluator`; rate limits, inference tables, EXECUTE grants and guardrail policies
   are set in the Unity Gateway UI (docs/SETUP.md, Part C). Judges use the pay-per-token
   `databricks-claude-haiku-4-5` endpoint; test-question writing uses Sonnet 4.5.
4. App service principal with `USE CATALOG`, `CREATE SCHEMA`, `MANAGE` on the catalog. The
   `_platform` schema must exist before the first deploy (the app's telemetry tables are created
   in it): `databricks schemas create _platform <catalog>`.
5. **Agent service principal** (writes logs, reads runtime config): secret scope
   `chatbot-factory` with `agent-sp-client-id` and `agent-sp-client-secret`. Pass its
   application ID as `--var agent_principal=...`.
6. **Identity keys (Security-only)**: secret scope `chatbot-factory-identity` with
   `pseudonym-hmac-key` (random bytes) and `identity-fernet-key`
   (`Fernet.generate_key()`). Only the Security group may read this scope.
7. Groups: `mlops` (admin), `security` (raw logs, break-glass), `support` (masked by ABAC policies).
8. Private git repos: Unity Catalog secret `<catalog>._platform.git_token` (REST API), with
   `READ SECRET` for the job identity.
9. Databricks CLI ≥ 0.279 (direct deployment engine), MLflow ≥ 3.14 (UC traces).
10. Previews and admin settings (Admin › Setup checklist tracks them): MLflow Review Queues;
    dashboard embedding allowed for `*.databricksapps.com`; unified Unity Gateway trace table;
    a serverless usage policy per environment, tagged `component` and `environment`; app instance
    count for scaling.

## Deploy

Targets are `qa` (catalog `qa_chatbot_factory`) and `prod` (catalog `prod_chatbot_factory`), each in its own
workspace. Workspace identifiers (warehouse, agent service principal, usage policy) are kept out
of the repository in `.databricks/bundle/<target>/variable-overrides.json`; copy
`variable-overrides.example.json` there and fill it in (docs/SETUP.md, A11).

```bash
databricks bundle deploy -t qa
databricks bundle run setup_platform -t qa
databricks bundle run deploy_agent -t qa
databricks bundle run chatbot_factory -t qa
```

Prod: run `run_evals bot_id=all` in QA on the commit, then deploy the same commit with
`-t prod`. `deploy_agent` refuses unless every live QA chatbot, and at least one, passed its
quality check for that platform version and commit.
Existing prod resources can be adopted with `databricks bundle deployment bind`.

## Local development

The project is managed with [uv](https://docs.astral.sh/uv/); `uv.lock` pins every dependency.

```bash
uv sync                 # create .venv with the library and the dev tools
uv run pytest           # run the unit tests
uv sync --extra app     # also install what the Streamlit app needs
uv run --extra app pytest   # the unit tests plus every page of the app, run against a pretend workspace
```

Databricks Apps installs from `requirements.txt`, so keep it in step with the `app` extra in
`pyproject.toml`.

## The app: who sees what, and how it stays quick

Each page works at the level that makes sense for it:

| Page | Level | Why |
|---|---|---|
| Home | All of a person's chatbots | One card per chatbot with its next step and a button straight to it |
| Create chatbot | One chatbot | Creates or finishes one |
| Documents | Both | "All my chatbots" lists every document read-only; choosing a chatbot opens Add, Curate and Review |
| Test questions | One chatbot | Each chatbot is checked against its own questions |
| Launch | One chatbot | A version is tested, approved and published per chatbot |
| Chat | One chatbot | A conversation is with one chatbot |
| Monitoring | Both | "All my chatbots" compares them and says which needs a look; choosing one opens its charts |
| Traces | One chatbot | A flow is one chatbot's steps |
| All chatbots (MLOps) | Both | Everything combined, or filtered to one |
| Admin (MLOps) | Platform | Settings, reports, costs and alerts are platform-wide; Chatbots and Lifecycle act on one chatbot |

Owners are told what to do rather than what happened. `factory/journey.py` turns a chatbot's
state, documents, test questions and test version into the path to launch with exactly one
current step (Home and Launch show it). `factory/problems.py` turns an error, an alert or a
failed quality check into what it means and what to do; the technical text is kept, folded
away, for whoever it is sent to. A part of a page that can't be loaded costs that part, not
the page.

Alerts say what is true now. Every check raises its alert again each period for as long as it
keeps finding the problem, so an alert that hasn't been raised again for a day or two (or whose
chatbot has since finished setup) no longer counts as open (`monitoring.alert_is_current`); the
monitor job and the Admin page close those. Alert messages are one plain line
(`problems.brief`); owners see a sentence about what to do, and only for alerts they can act
on. The rest is one quiet line for them and a list for MLOps under Admin, section Alerts.

Admin, section Setup checklist tracks the setup that is finished in Databricks screens
(`factory/setup_steps.py`). What the app can check for itself is marked done or to do, with
what to do; what it can't see is a box for MLOps to tick, saved in the `setup_confirmed`
setting with who ticked it. An empty box therefore always means something is still to do.

A page never says a background job is running because a button was pressed. Starting a job
notes its run id in the audit log (`job_started`), and every page then asks Databricks where
that run stands (`common.watch_job`, `factory/jobruns.py`): waiting, running with a progress
bar measured against how long the job's recent runs took, or stopped with an error and what to
do. The bar updates itself every 15 seconds, the page reloads once when the job ends, and the
button that started it stays locked until then, for everyone and across a browser refresh.
An update of the test version is three steps (update, choose the search method, quality check),
shown as such, and the manual quality check is locked while one is going because a check is
already on its way. A run that stops shows the failed task's own error (not "see run output"),
says what it left behind (never "nothing was changed"), and says whether starting it again can
help or the job's setup needs MLOps.

A running job can be stopped from the page that shows it. The "usually takes" time ignores runs
that found nothing to do. The quality check paces its judge calls (`quality.check` settings),
tests the judge model with one question before it starts, and records each question as one
MLflow trace. When more questions are approved than `quality.check.max_questions` (50), it
checks a fixed sample of them, spread over the documents and kinds of question
(`evals.sample_cases`); "at least 50 approved" still counts every approved question. A
diagnostic run (`--diagnose true`, started from Admin) times each dependency separately and
dumps what every thread is doing when something stalls; `--diagnose paced` does the same at the
check's normal pace, to test the load. A question the chatbot answers with its error message is
a fault, not an answer: the check asks one question before it starts and stops with the reason,
and any such question fails the check outright.

Jobs are separate processes and don't share the app's or the agent's environment. Each task is
passed the git commit, the environment and the SQL warehouse ID as parameters, and
`jobs/_bootstrap.py` turns them into `FACTORY_GIT_COMMIT`, `FACTORY_ENV`, `FACTORY_WAREHOUSE_ID`
and `MLFLOW_TRACING_SQL_WAREHOUSE_ID` (MLflow reads traces stored in Unity Catalog through that
warehouse).

Streamlit runs a page again on every click, so the app avoids asking twice:

- Page reads are remembered for 30 seconds (`READ_TTL` in `app/common.py`) and forgotten on any
  write, so people see their own change at once. Background jobs' results show within 30
  seconds, or at once with **Refresh** in the menu. The services that decide before they write
  (`ControlPlane`, `Documents`) always read fresh.
- Independent reads run at the same time (`read_many`), and the same read across chatbots is one
  statement (`across_bots`).
- Pages with sections (Documents review, Monitoring, Admin) load only the open section.
- Writes are batched: a batch of rows travels as one JSON parameter (`factory.sql.json_rows`).
  UPDATE and DELETE pick their rows with a plain parameter list (`factory.sql.in_list`), never
  a subquery. Admin, section Setup checklist has a button that checks the warehouse accepts the
  batched form.
- A background job is not queued again when an identical run is still waiting to start
  (`factory/jobruns.py`); the ingest job runs one at a time for the whole platform.

## Lifecycle and admin

Admin › Lifecycle restores archived chatbots, deletes them (stops serving, data kept), and
restores or purges deleted ones; purges also happen automatically after
`soft_delete_retention_days`. Purging drops the bot schema, its index rows and its auto-ingest
job; audit history, request logs and traces stay for 7 years. Admin › Reports lists every bot
with usage, cost, quality and alerts (CSV download) and flags idle bots for archiving.

## Observability

See `docs/ARCHITECTURE.md` → Observability. `deploy_agent` creates the UC-backed traces experiment,
registers the prompt and applies the gateway model services, then `setup_observability`
registers the production judges, drift monitors, metric views and the Genie agent. The app's **Monitoring** page charts speed, TTFT,
cost, quality, guardrails, retrieval and question drift per chatbot, with links to MLflow and the drift
dashboard. **Traces** shows each chatbot's typical step-by-step flow (timing spread, cost and the code
behind every step) and, for MLOps and Security, any single trace in full. **All chatbots** (MLOps)
combines every chatbot or filters to one: per-step latency, a slow-answer heatmap, question topics
worst first, slice comparison, document gaps, conversation health, guardrails and a paginated
table of every chatbot. The design canvas compares these with Arize; see docs/ARCHITECTURE.md.

## Verify against a live workspace first

The code is unit-tested offline only. October 2026 additions to verify: model-service REST
body and the `/ai-gateway/mlflow/v1` call with `model=<catalog.schema.service>`; the shape of a
service-policy DENY (`databricks_service_policy`) for streaming and non-streaming; streaming
through model services; `mlflow.genai.evaluate` with `model_id=`, the `trace.search_spans`
API and multi-Feedback code scorers; ConversationSimulator import path and predict signature;
Review Queues SDK on managed MLflow; `optimize_prompts` result fields; managed-sessions SDK
calls (`factory/memory.py`, falls back silently); ABAC `CREATE POLICY` and `CREATE GOVERNED TAG`
syntax; Data Classification and anomaly-detection REST bodies; Genie `create_space`;
metric-view `filter`; system-table column names in `sql/system_views.sql`; the app
`telemetry_export_destinations` and `compute_size` fields; `agents.deploy(budget_policy_id=)`.
App (October 7): the Review queue link path `/ml/experiments/<id>/reviews`; `queue_traces` from the app's service principal (needs Can Edit on the traces experiment); `mlflow.search_traces(experiment_ids=, filter_string="tags.bot_id = ...",
return_type="list")` and `mlflow.get_trace` from the app's service principal (Traces and All
chatbots pages); span `inputs`/`outputs` sizes in the One trace view; Streamlit ≥ 1.65 features
(`st.dataframe` row selection, `bar_chart(horizontal=, stack=)`, `NumberColumn(format="percent")`,
`help=` on titles and markdown, `width="stretch"`, `st.segmented_control`, `st.popover`).
October 11 (speed and clarity pass, checked only against a pretend workspace): batches sent as
`inline(from_json(:rows, 'ARRAY<STRUCT<...>>', map('mode', 'FAILFAST')))` with one named parameter
through the Statement Execution API and `spark.sql(args=)`; `MERGE ... USING` and
`IN (SELECT ... FROM <that subquery>)` over it; `UNION ALL` across chatbot schemas with a named
parameter per branch; GRANT statements for different objects running side by side;
`jobs.list_runs(active_only=True)` returning each run's state and `job_parameters` (if it
doesn't, jobs are simply started as before); the kept AI Search index handle in the agent
(refetched once when a search fails); section ids that now end in a hash of the text (existing
sections keep their ids until their document is next read again). Observability checks: MLflow ≥ 3.14 on the serving
image; traces reach `traces_otel_spans` and show in the experiment; streamed calls through the
guardrailed endpoint (or the logged fallback); `mlflow.chat.tokenUsage` on the autolog spans;
built-in scorers accept `model=` (else they use the managed judge); scorer filter strings on
tags; `search_traces(return_type="list")` assessments shape; labeling-session and
`link_traces_to_run` APIs; `ai_query` returning an embedding array; monitor output table and
column names used by the Monitoring page; OTel column names in the retention delete. Also check: `w.alerts_v2` request shape (`factory/alerting.py`);
the `DatabricksReranker` import path and `similarity_search(reranker=...)`; AI Search filter
operators (`"effective_ts <="`); `ai_prep_search` output fields; `agents.deploy` OBO scopes and
`custom_outputs` round-trip; agent SP write grant on the logs volume; COPY INTO into
`request_events` / `health_events`.

**Cost note:** the agent never touches the SQL warehouse on the hot path except UC access
probes (cached 15 min per user). The 15-minute monitor job and SQL email alerts do use the
warehouse; widen the schedule if warehouse cost matters more than alert latency.

## Not built (day 2 / maybes)

Document-level access enforcement (data model ships: `user_access`, scopes on chunks; ABAC is
the planned mechanism), Excel/CSV path, SharePoint (Lakeflow Connect is now GA), instructed
retrieval (ARC-7), Agent Runtime hosting (revisit at GA; see docs/ARCHITECTURE.md).
