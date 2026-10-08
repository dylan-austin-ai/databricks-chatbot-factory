# Chatbot Factory setup guide

This guide covers every step to get Chatbot Factory running in a Databricks workspace, in order.
Steps marked **[UI]** are done by clicking in Databricks because no API exists for them yet.
Steps marked **[CLI]** are commands you run on your laptop. Steps marked **[App]** are done in
the Chatbot Factory app once it's running.

Who can do each step:

| Role | Needed for |
|---|---|
| **Account admin** | Usage policy and budgets (account console) |
| **Metastore admin** | Unified trace table; making the deploy identity the catalog owner |
| **Workspace admin** | Previews, groups, embed policy, secret scope permissions |
| **MLOps (you)** | Everything else |

Set aside about half a day for the first environment (dev). Prod repeats Parts B–F with
`-t prod`.

Values used below (change them if yours differ):

| Name | Dev | Prod |
|---|---|---|
| Catalog | `chatbots_dev` | `chatbots` |
| Platform schema | `_platform` | `_platform` |
| Agent endpoint | `chatbot-agent` | `chatbot-agent` |
| App | `chatbot-factory` | `chatbot-factory` |
| Traces experiment | `/Shared/chatbot-factory/dev/traces` | `/Shared/chatbot-factory/prod/traces` |

---

## Part A: Prerequisites (before the first deploy)

### A1. Check the workspace [UI] (workspace admin)

1. The workspace has Unity Catalog, serverless compute (jobs and SQL), Model Serving, AI Search
   (formerly Vector Search) and Databricks Apps.
2. Go to **Serving**. Confirm these pay-per-token endpoints are listed and **Ready**:
   - `databricks-claude-haiku-4-5`: answers, quality judges and the guardrail evaluator
   - `databricks-claude-sonnet-4-5`: writes test questions; fallback answer model
   - `databricks-gte-large-en`: embeddings for question drift

   If one is missing, the region doesn't offer it. Pick another model and change it in
   `src/factory/platform_defaults.yml` under `models:` before you deploy.

### A2. Turn on previews [UI] (workspace admin)

Click your user name (top right) › **Settings** › **Previews** (also called **Manage
previews**). Search for each of these and switch it **On**:

| Preview | Used for |
|---|---|
| **MLflow Review Queues** | Failed production answers go to a review queue for each chatbot |
| `ai_parse_document` / AI Functions document parsing (v2) | Document parsing during ingestion |
| `ai_prep_search` | Chunking and metadata extraction |
| **Data Classification** (if listed; it's GA in most regions) | Automatic personal-data tagging of the catalog |
| **Data quality monitoring: anomaly detection** (if listed) | Freshness checks on the platform tables |
| **Agent Bricks: managed sessions** (optional) | Conversation memory. If you don't turn this on, set `history.store: none` in Admin › Platform later; otherwise memory just falls back to the client's history |

If a preview isn't listed, ask your Databricks account team to enable it. The related setup step
prints "not available" and carries on. Nothing else breaks.

### A3. Create groups [UI] (workspace admin)

**Settings** › **Identity and access** › **Groups** › **Manage** › **Add group**. Create:

| Group | Members | What it can do |
|---|---|---|
| `mlops` | Platform admins (you) | Admin page, raw logs, traces, all settings |
| `security` | Security team | Raw logs, traces, break-glass identity keys |
| `support` | Help-desk staff | Request logs with personal data masked |

These names are the defaults in `access:` in `platform_defaults.yml`. Keep them or change both.

### A4. Create the SQL warehouse [UI]

**SQL Warehouses** › **Create SQL warehouse**: type **Serverless**, size **Small**, auto stop
**10 minutes**. Open it and copy the **ID** from the **Overview** tab, or from the end of the
URL. This is `<warehouse_id>`.

### A5. Create the catalog and its owner [UI] (metastore admin)

1. **Catalog** › **+** › **Create a catalog**. Name `chatbots_dev`, type **Standard**, pick the
   storage location your company uses, then **Create**.
2. Decide which identity deploys the bundle and runs the jobs (the *deploy identity*):
   - **Dev:** your own user is fine.
   - **Prod:** use a service principal (create one in **Settings** › **Identity and access** ›
     **Service principals** › **Add service principal**), and deploy prod from CI or with
     `databricks auth login` as that principal.
3. Open the catalog › **Permissions**, or click the owner name next to **Owner**. Make the deploy
   identity the **Owner** of the catalog. Jobs create schemas, AI Search indexes, model services
   and views inside it, and later hand the platform schema to the app.
4. Create the platform schema, as the deploy identity, before the first deploy **[CLI]** (or
   **Catalog** › `chatbots_dev` › **Create schema**, name `_platform`):

   ```bash
   databricks schemas create _platform chatbots_dev
   ```

   The app sends its own telemetry to tables in this schema, and Databricks creates those tables
   when the app is deployed. The schema has to exist by then, and the deploy identity needs
   **MANAGE** on the catalog and schema plus **CREATE TABLE** on the schema; it has all three as
   the catalog owner. `setup_platform` (B2) fills in the rest of the schema.
5. Write down the deploy identity:
   - For a user, their email.
   - For a service principal, its **Application ID**.

   This is `<jobs_run_as>`.

### A6. Create the agent service principal [UI] + [CLI]

The shared agent endpoint runs as this principal. It reads runtime config and writes logs.

1. **Settings** › **Identity and access** › **Service principals** › **Add service principal** ›
   **Add new**. Name it `chatbot-factory-agent`.
2. Open it and copy the **Application ID**. This is `<agent_principal>`.
3. Open the **Secrets** tab › **Generate secret**, set a lifetime of 730 days, and copy the
   **Client ID** and **Secret**.
4. Store them in a secret scope **[CLI]**:

   ```bash
   databricks secrets create-scope chatbot-factory
   databricks secrets put-secret chatbot-factory agent-sp-client-id --string-value '<client id>'
   databricks secrets put-secret chatbot-factory agent-sp-client-secret --string-value '<secret>'
   databricks secrets put-acl chatbot-factory mlops MANAGE
   databricks secrets put-acl chatbot-factory <jobs_run_as> READ   # deploy_agent wires them into the endpoint
   ```

5. Put a calendar reminder 30 days before the secret expires. Rotate it by repeating steps 3–4,
   then rerun `deploy_agent`.

### A7. Create the identity keys (Security-only scope) [CLI] (Security team)

These keys pseudonymize users in logs and encrypt the real identity for break-glass. Only
Security may read them.

```bash
python3 -c "import secrets; print(secrets.token_hex(32))"                                # pseudonym key
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"  # encryption key

databricks secrets create-scope chatbot-factory-identity
databricks secrets put-secret chatbot-factory-identity pseudonym-hmac-key  --string-value '<pseudonym key>'
databricks secrets put-secret chatbot-factory-identity identity-fernet-key --string-value '<encryption key>'
databricks secrets put-acl chatbot-factory-identity security MANAGE
databricks secrets put-acl chatbot-factory-identity <jobs_run_as> READ     # deploy_agent wires them into the endpoint
databricks secrets list-acls chatbot-factory-identity                       # check: nobody else listed
```

Keep a copy of the encryption key in the Security team's vault. Without it, break-glass
identities can't be decrypted.

### A8. Git token (only if chatbots read documents from private git repos) [UI]

1. Create a read-only token in your git host for a bot account.
2. **Catalog** › `chatbots_dev` › `_platform` › **Create** › **Secret**. This needs the platform
   schema, so do it after step C1 (the first run of `setup_platform`). Name it `git_token` and
   paste the token as the value.
3. On the secret's **Permissions** tab, grant **READ SECRET** to `<jobs_run_as>`.
4. If serverless egress is restricted, add the git host to the workspace's serverless network
   policy: **Settings** › **Security** › **Network** › **Serverless egress control**.

### A9. Serverless usage policy [UI] (account admin)

This tags every job, the app and the agent endpoint, so their cost shows up in
`system.billing.usage`.

1. Click your user name › **Settings** › **Compute** › next to **Serverless usage policies**,
   click **Manage** › **Create**.
2. Name `chatbot-factory`, select the workspace, and add the custom tag `component` =
   `chatbot-factory` › **Create**.
3. On the policy's **Permissions** tab, use **Grant access** to give **User** to `<jobs_run_as>`,
   the agent service principal and (after B2) the app's service principal.
4. Copy the policy ID from the browser URL when the policy is open. This is
   `<usage_policy_id>`. If you skip this step, leave the variable empty.

### A10. Account budget [UI] (account admin)

1. Account console (accounts.cloud.databricks.com) › **Usage** › **Budgets** › **Add budget**.
2. Name `chatbot-factory monthly`. Under **Workspaces**, pick this workspace. Under **Resource
   tags**, add `component` = `chatbot-factory`.
3. Add thresholds at 70%, 90% and 100% of your monthly cap, each emailing MLOps. Tick **Block
   usage** on the 100% threshold only if a hard stop is acceptable. Blocking is approximate and
   doesn't interrupt requests already running.
4. Optional, for a second budget limited to Unity Gateway model spend: set **Resource types**
   to **Unity Gateway** and set a **per-user limit** (for example $50/month). Users who hit it
   are blocked from the answer model.

Each chatbot's own monthly budget is set in the creation wizard. The factory enforces it from
tagged usage, separately from these account budgets.

### A11. App catalog and environment (nothing to edit)

The app's command and environment variables are defined in `resources/app.yml` under `config:`.
`FACTORY_CATALOG` comes from the bundle's `catalog` variable and `FACTORY_ENV` from the target
name, so `-t dev` gives `chatbots_dev` / `dev` and `-t prod` gives `chatbots` / `prod`. To use a
different catalog, change `catalog` for the target in `databricks.yml` or pass
`--var catalog=<name>` on deploy.

### A12. Install the CLI

Databricks CLI **0.279 or newer**. The bundle uses the direct deployment engine.

```bash
databricks --version
databricks auth login --host https://<your-workspace>.cloud.databricks.com
```

---

## Part B: First deploy

### B1. Deploy the bundle [CLI]

```bash
cd chatbot-factory
databricks bundle validate -t dev --var warehouse_id=<warehouse_id> \
  --var agent_principal=<agent_principal> --var usage_policy_id=<usage_policy_id>
databricks bundle deploy -t dev --var warehouse_id=<warehouse_id> \
  --var agent_principal=<agent_principal> --var usage_policy_id=<usage_policy_id>
```

The first deploy may stop with an error that serving endpoint `chatbot-agent` doesn't exist,
because the app is bound to it and it's created in B3. The jobs are created before that point.
Carry on with B2 and B3, then deploy again in B4.

An error that mentions the app's telemetry tables or `_platform` is a different problem: the
platform schema from A5 step 4 doesn't exist yet, or the deploy identity lacks the permissions
listed there. Fix that and deploy again.

### B2. Create the platform tables [CLI]

```bash
databricks bundle run setup_platform -t dev
```

This creates the platform schema and tables, ABAC column masks, cost views and the shared AI
Search index. It also turns on data classification and anomaly detection, and publishes
settings for the agent. Read the output: any line ending in "not available" is a preview from
A2 that isn't on yet. Turn it on and rerun this step; nothing else is affected.

### B3. Deploy the agent [CLI]

```bash
databricks bundle run deploy_agent -t dev
```

The job prints numbered steps. It first checks the prerequisites from Part A that it can't
build (catalog, SQL warehouse, model endpoints, both secret scopes) and stops with the full
list if any are missing. It then makes sure everything the agent depends on exists, in order,
before deploying it: the control plane tables, the AI Search endpoint and shared index, and the
agent's settings file. On a new workspace the AI Search step waits for the endpoint and index to
finish provisioning, which can take 15 minutes or more.

It then does six things:

- creates the traces experiment, stored in Unity Catalog
- registers the answer prompt
- creates the Unity Gateway model services `chatbots_dev._platform.answer_haiku` (Haiku 4.5 with
  a Sonnet 4.5 fallback) and `chatbots_dev._platform.guardrail_evaluator`
- deploys the `chatbot-agent` endpoint
- registers the production judges, drift monitors, metric views and the Genie space
- prints a checklist of the Unity Gateway settings for step C2. Keep that output open.

### B4. Deploy again and start the app [CLI]

```bash
databricks bundle deploy -t dev --var warehouse_id=<warehouse_id> \
  --var agent_principal=<agent_principal> --var usage_policy_id=<usage_policy_id>
databricks bundle run chatbot_factory -t dev
```

### B5. Give the app its catalog rights [UI]

1. **Compute** › **Apps** › `chatbot-factory` › **Authorization** (or **Overview**). Copy the
   **service principal** Application ID. This is `<app_sp>`.
2. **Catalog** › `chatbots_dev` › **Permissions** › **Grant**, to `<app_sp>`:
   **USE CATALOG**, **USE SCHEMA**, **SELECT**, **MODIFY**, **CREATE SCHEMA**,
   **CREATE TABLE**, **READ VOLUME**, **WRITE VOLUME**, **EXECUTE**.
3. If you created a usage policy (A9), give `<app_sp>` **User** on it.

---

## Part C: Unity Gateway and the other UI-only settings

### C1. Tell the factory who's who [App]

1. Open the app (**Compute** › **Apps** › `chatbot-factory` › the URL). Sign in as a member of
   `mlops`.
2. Go to **Admin** › **Platform**. In the YAML editor, set:

   ```yaml
   access:
     app_service_principal: <app_sp>
     jobs_run_as: <jobs_run_as>
   ```

3. Click **Save settings**.
4. Rerun the platform setup so the app becomes owner of the platform schema. The app and the
   jobs are also added to the people who see unmasked text, because the judges and the drift
   job need the real questions.

   ```bash
   databricks bundle run setup_platform -t dev
   ```

### C2. Configure the answer model service [UI]

In the workspace sidebar, click **AI Gateway** › **Models** tab › `chatbots_dev._platform.answer_haiku`.
The exact values are also on **Admin** › **Setup checklist** in the app.

**Permissions**
1. Open the **Permissions** tab › **Grant**. Select the agent service principal
   (`chatbot-factory-agent`), tick **EXECUTE**, then click **Grant**.
2. Repeat for the `mlops` group (playground testing) and for `<jobs_run_as>`.

**Rate limits**
3. Find the **Rate limits** section (on the service page or under **Edit**). Add:
   - **Service** (whole endpoint): **600** requests per **minute**
   - **Per user** default: **30** requests per **minute**
4. Save. A caller over the limit gets HTTP 429, which the factory logs as an error with the
   reason.

**Inference table**
5. Find **Inference table** (or **Inference logging**). Turn it on with **Catalog**
   `chatbots_dev`, **Schema** `_platform` and **Table prefix** `gw_answer_haiku`. Save.
6. Copy the full table name the page shows, for example
   `chatbots_dev._platform.gw_answer_haiku_payload`. This is `<inference_table>`.

**Guardrail policies.** Open the **Policies** tab › **New policy** and create one policy per row:

| Policy (guardrail) | Phase | Mode | Advanced options › Evaluator | Why |
|---|---|---|---|---|
| **Unsafe Content** (`system.ai.block_unsafe_content`) | Input and Output | **Enforce** | default | Harmful content |
| **Jailbreak** (`system.ai.block_jailbreak`) | Input | **Enforce** | default | Jailbreak and prompt injection |
| **Hallucination** (`system.ai.block_hallucination`) | Output | **Log** | `chatbots_dev._platform.guardrail_evaluator` | Grounding check. Watch it for two weeks, then switch to Enforce |
| **Sensitive data** (`system.ai.detect_sensitive_data`) | Input and Output | **Enforce** (block or redact) | n/a | SSNs, cards, IBANs and other IDs |

For each policy:

7. Click **New policy**. Enter a name, for example `jailbreak`.
8. Leave **Applied to** as all principals.
9. Pick the **Guardrail type** from the table.
10. Tick the **Phase** from the table.
11. Choose the **Mode**.
12. For Hallucination only, open **Advanced options** and set the evaluator model service to
    `chatbots_dev._platform.guardrail_evaluator`.
13. Leave **Rank** as is, then click **Create policy**.

What this gives you:
- **Enforce** blocks come back to the agent as a normal reply marked blocked. The agent shows
  the person a generic "can't help with that" message and records the block in three places:
  `request_log.outcome = 'blocked'`, a `guardrail_events` row (rule `gateway_guardrail`, with
  the reason), and a `guardrail.gateway` span on the trace.
- **Log** verdicts never reach the person or the agent. Databricks writes them to the
  evaluator's inference table, set up in C3, and the factory exposes them as
  `v_guardrail_verdicts`.

**Tag**
14. On the service's **Overview** (or **Details**) tab › **Tags**, add `component` =
    `chatbot-factory`.

### C3. Configure the guardrail evaluator [UI]

**AI Gateway** › **Models** › `chatbots_dev._platform.guardrail_evaluator`:

1. **Permissions** › **Grant** › `mlops` › **EXECUTE**. If the policy attach step says the
   evaluator needs query access, also grant it to whoever attached the policies.
2. **Inference table** on, with **Catalog** `chatbots_dev`, **Schema** `_platform` and **Table
   prefix** `gw_guardrail_evaluator`. Copy the full table name. This is
   `<evaluator_inference_table>`.
3. **Tags**: `component` = `chatbot-factory`.
4. No rate limits or policies here.

### C4. Unified trace table [UI] (metastore admin)

1. **AI Gateway** › **Govern** › **Traces** › **Set up tracing**.
2. Choose catalog `chatbots_dev`, schema `_platform` › **Save**.
3. Copy the table name, for example `chatbots_dev._platform.unity_gateway_otel_spans`. This is
   `<trace_table>`.

### C5. Record the table names and finish observability [App] + [CLI]

1. **Admin** › **Platform**. Set:

   ```yaml
   unity_gateway:
     inference_table: <inference_table>
     evaluator_inference_table: <evaluator_inference_table>
     trace_table: <trace_table>
   ```

2. Click **Save settings**.
3. Rerun only the observability task:

   ```bash
   databricks bundle run deploy_agent -t dev --only observability
   ```

   If your CLI doesn't support `--only`, open **Jobs** › **[factory] deploy shared agent** ›
   **Run now** on the `observability` task (task menu › **Run task**).

This creates two views and grants them to `mlops` and `security`:
- `v_gateway_calls`: every gateway call, joined to our `request_log` by `request_id`
- `v_guardrail_verdicts`: every Log-mode verdict with flagged, confidence and reason, joined by
  `request_id` and `bot_id`

The **Monitoring** › **Guardrails** tab charts both.

### C6. Allow the drift dashboard inside the app [UI] (workspace admin)

1. Click your user name › **Settings** › **Security** › **External access**.
2. Under **Embed dashboards**, choose **Allow approved domains** › **Manage**.
3. Enter `*.databricksapps.com` › **Add domain** › **Save**.

### C7. Share the Genie space [UI]

1. **Genie** › open **Chatbot Factory observability**, created by B3.
2. Click **Share**. Add `mlops` with **Can run** (and `security` if wanted).

The app links to it from **Monitoring**.

### C8. Scale the app [UI]

1. **Compute** › **Apps** › `chatbot-factory` › **Edit** › **Configure** step.
2. Confirm **Compute size** is **Medium**.
3. Tick **Enable horizontal scaling** and set **Number of instances** to **2** for prod (1 for
   dev) › **Save**. The app is stateless, so any instance can serve any user.

### C9. Review queue access [UI]

Review queues live in the traces experiment, on the **Reviews** tab.

1. **Experiments** › `/Shared/chatbot-factory/dev/traces` › **Permissions**. Give each
   chatbot's Reviewer **Can read**, and `mlops` **Can manage**. Give the app's service principal (`<app_sp>`) **Can edit**, so
   All chatbots › Gaps can send questions to review queues.
2. Traces are stored in Unity Catalog, and only `mlops` and `security` can read the trace
   tables. A Reviewer outside those groups sees the queue but not the trace contents. Either
   add Reviewers to `security`, or have MLOps work the queues.

### C10. Per-chatbot MLflow screens (after each chatbot goes live) [UI]

**Admin** › **Setup checklist** lists, for each live chatbot, the screens to set up in MLflow
(no API exists for these):

- **Issue detection:** in the traces experiment, open **Traces**, filter on
  `tags.bot_id = '<bot_id>'`, and run issue detection (Beta) on the filtered traces.
- **Custom trace view:** in the same filtered view, choose the columns (request, response,
  latency, tokens, cost, judge scores) and save the view as `<bot_id>`.

---

## Part D: Load users and check everything works

### D1. User access list [App]

**Admin** › **User access** › upload `user_access.csv` with these columns:

```
user_id,country,state,business_function,security_scopes,active,valid_from,valid_to
jane@corp.com,US,TX,Claims,general;claims_pii,true,2026-01-01,
```

`security_scopes` is separated by semicolons. Rows replace any existing rows for the same user.

### D1b. Let everyone at the company use chatbots [UI] (account and workspace admins)

Databricks Apps only serve people who sign in to Databricks; there is no anonymous access. To let
every employee use a chatbot set to **Everyone at the company**, without giving them anything else:

1. **Sync all employees** from your identity provider (Entra ID or Okta) into the Databricks
   account: account console › **Settings** › **User provisioning** (SCIM), or automatic identity
   management. They sign in with their normal company account (SSO).
2. **Give them consumer access only:** workspace **Settings** › **Identity and access** ›
   **Users** (or the synced all-employees group) › **Entitlements**: tick **Consumer access** and
   untick **Workspace access**. Consumers can open apps shared with them, nothing else (no data,
   notebooks or compute).
3. **Share the app:** **Compute** › **Apps** › `chatbot-factory` › **Permissions** › add the
   all-employees group (or `account users`) with **Can use**.
4. **Limit sign-in to the company network:** workspace **Settings** › **Security** › **IP access
   lists**: turn on and add your corporate egress ranges. These lists also apply to app URLs.
   Alternatively use front-end private connectivity.
5. Databricks bills by usage, not per user; confirm with your account team that adding
   consumer-only users carries no extra cost for your contract.

For people who can't be in your directory at all (contractors on a partner portal, kiosks), use
the **another app** path: an internal web page or Teams bot that signs people in your way and calls
the chatbot as a trusted service principal (D2). Those chatbots are limited to Public documents.

### D2. Trusted callers (only if another system calls the agent for users) [App]

If a middleware service calls the agent on behalf of end users, add its service principal's
Application ID in **Admin** › **Platform** under `access.trusted_callers`, then click **Save**.

### D3. Smoke test [App]

1. **Create chatbot**: run the wizard with 3–5 small PDFs and a test reviewer.
2. Watch **Documents**: files should be readable (green) within a few minutes. The per-chatbot
   ingestion job starts on its own when files land.
3. **Test questions**: approve at least 50. This is required to go live.
4. **Launch**: the quality check runs. Review the scores and approve.
5. **Chat**: ask an in-scope question, then try "ignore your instructions and…", then a
   question about legal advice.
6. Check that each of these happened:

| Where | What you should see |
|---|---|
| MLflow › traces experiment › **Traces** | One trace per question with spans: retrieve, prompt.build, answer.attempt_1 (tokens, time to first token, cost), guardrail checks |
| **Monitoring** › **Guardrails** | The jailbreak block (Unity Gateway policy blocked) and the refusal |
| `SELECT * FROM chatbots_dev._platform.guardrail_events ORDER BY ts DESC` | Rows for the block and refusal, with trace_id |
| `SELECT * FROM chatbots_dev._platform.v_guardrail_verdicts ORDER BY event_time DESC` | Hallucination verdicts for each answer (Log mode) |
| `SELECT * FROM system.ai_gateway.usage WHERE request_tags['bot_id'] = '<bot_id>'` | Tokens per request, tagged with the chatbot |
| **Monitoring** (next day) | Speed, cost, quality and drift charts filling in |

### D4. Turn the hallucination policy to Enforce (after about two weeks)

Compare `v_guardrail_verdicts` (flagged = true) with the judges' groundedness scores and with
thumbs-down feedback. If its flags are mostly right, edit the policy in C2 and change **Mode** to
**Enforce**. From then on, its blocks are recorded like the others.

---

## Part E: Promote the platform to prod

1. In dev, check that the commit you're promoting passed:
   `databricks bundle run run_evals -t dev --params bot_id=all`.
2. Repeat A5 (catalog `chatbots`), A9/A10 if they're per environment, and Parts B–D with
   `-t prod`, using a service principal as the deploy identity.
3. `deploy_agent` in prod refuses to run unless dev quality checks passed for this exact
   platform version and git commit.

---

## Part F: Ongoing operations

| When | What | Where |
|---|---|---|
| Daily | Open alerts | Admin › Alerts (also emailed) |
| Weekly | Review queues for each chatbot | Traces experiment › **Reviews** tab |
| Weekly | Costs vs gateway usage | Admin › Costs |
| Monthly | Idle chatbots (flagged after 45 days) | Admin › Reports › archive |
| As needed | Restore, delete, purge chatbots | Admin › Lifecycle (deleted chatbots are purged automatically after 90 days) |
| As needed | Optimize and promote the answer prompt | Admin › Answer prompt (every chatbot is re-checked afterwards) |
| Before expiry | Rotate the agent service principal secret | A6 steps 3–4, then `deploy_agent` |
| After a policy change | Keep the checklist in step | Admin › Setup checklist |

Scheduled jobs that run on their own:
- monitor: every 15 minutes in business hours, hourly otherwise
- production judging: 06:15
- drift: 05:45
- git sync: hourly
- maintenance: 02:30 (purges, retention)

---

## Things to verify on the first live deploy

The code has unit tests but hasn't run against a live workspace. Watch for these in the B2/B3
output and the first smoke test:

- The model service REST create and PATCH (`update_mask=comment,config.routing` must leave the
  UI settings from C2 alone). Check C2's settings are still there after rerunning `deploy_agent`.
- The blocked-reply shape (`databricks_service_policy`) for streaming and non-streaming answers.
- Whether the evaluator inference table's `request_id` matches the answer service's, which
  `v_guardrail_verdicts` joins on.
- The Review Queues, managed sessions and Genie API calls (each logs and carries on if it fails).
- The ABAC `CREATE POLICY` syntax and the system-table column names in `sql/system_views.sql`.
- README › "Verify against a live workspace first" has the full list.
