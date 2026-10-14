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

Set aside about half a day for the first environment (QA). Prod is a separate workspace and
repeats Parts A–F there with `-t prod`.

Values used below (change them if yours differ):

| Name | QA | Prod |
|---|---|---|
| Catalog | `qa_chatbot_factory` | `prod_chatbot_factory` |
| Platform schema | `_platform` | `_platform` |
| Agent endpoint | `chatbot-agent` | `chatbot-agent` |
| App | `chatbot-factory` | `chatbot-factory` |
| Traces experiment | `/Shared/chatbot-factory/qa/traces` | `/Shared/chatbot-factory/prod/traces` |
| Bundle target | `qa` | `prod` |

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
| **Agent Framework: On-Behalf-Of-User Authorization** (required) | How the agent learns who is asking: Model Serving hands it the asking user's credentials for each question. Without it the chatbot answers nobody |
| **Production monitoring for MLflow** (required) | The observability task (C5) starts the judges that score live answers with it: 20% of them, every one for critical chatbots |
| **Managed MLflow Prompt Registry** (required) | `deploy_agent` stores the answer prompt there and the chatbot reads it from there; `optimize_prompt` saves its suggestions there |
| **MLflow Review Queues** | Failed production answers go to a review queue for each chatbot |
| **AI Search: Full-Text Search** (optional) | Lets the search comparison also try keyword-only search ("Keyword search"). While it is off, that one option is skipped and the run's output says so |
| `ai_parse_document` / AI Functions document parsing (v2) | Document parsing during ingestion |
| `ai_prep_search` | Chunking and metadata extraction |
| **Data Classification** (if listed; it's GA in most regions) | Automatic personal-data tagging of the catalog |
| **Data quality monitoring: anomaly detection** (if listed) | Freshness checks on the platform tables |
| **Agent Bricks: managed sessions** (optional) | Conversation memory. If you don't turn this on, set `history.store: none` in Admin › Platform later; otherwise memory just falls back to the client's history |

If a preview isn't listed, ask your Databricks account team to enable it. The related setup step
prints "not available" and carries on. Nothing else breaks, with one exception: the chatbot does
not answer anyone until **Agent Framework: On-Behalf-Of-User Authorization** is on and the agent
has been deployed (B3) after that.

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

On the warehouse's **Permissions**, give **Can use** to `<jobs_run_as>` as well as to the app's
service principal (after B2). The jobs need it too: the traces are stored in Unity Catalog, and
MLflow reads them through this warehouse when the quality check collects its scores. The bundle
passes the ID to every job task as `--warehouse_id`, so after changing the warehouse, deploy the
bundle again.

### A5. Create the catalog and its owner [UI] (metastore admin)

1. **Catalog** › **+** › **Create a catalog**. Name `qa_chatbot_factory`, type **Standard**, pick the
   storage location your company uses, then **Create**.
2. Decide which identity deploys the bundle and runs the jobs (the *deploy identity*):
   - **QA:** your own user is fine.
   - **Prod:** use a service principal (create one in **Settings** › **Identity and access** ›
     **Service principals** › **Add service principal**), and deploy prod from CI or with
     `databricks auth login` as that principal.
3. Open the catalog › **Permissions**, or click the owner name next to **Owner**. Make the deploy
   identity the **Owner** of the catalog. Jobs create schemas, AI Search indexes, model services
   and views inside it, and later hand the platform schema to the app.
4. Create the platform schema, as the deploy identity, before the first deploy **[CLI]** (or
   **Catalog** › `qa_chatbot_factory` › **Create schema**, name `_platform`):

   ```bash
   databricks schemas create _platform qa_chatbot_factory
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

The shared agent endpoint runs as this principal. It reads runtime config, writes logs and
searches the AI Search indexes (`deploy_agent` grants it SELECT on them and Can Use on the AI
Search endpoint; provisioning grants each confidential chatbot's own index).

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
2. **Catalog** › `qa_chatbot_factory` › `_platform` › **Create** › **Secret**. This needs the platform
   schema, so do it after step C1 (the first run of `setup_platform`). Name it `git_token` and
   paste the token as the value.
3. On the secret's **Permissions** tab, grant **READ SECRET** to `<jobs_run_as>`.
4. If serverless egress is restricted, add the git host to the workspace's serverless network
   policy: **Settings** › **Security** › **Network** › **Serverless egress control**.

### A9. Serverless usage policy [UI] (account admin)

This tags every job, the app, the agent endpoint and the AI Search endpoint, so their cost shows
up in `system.billing.usage`. Create **one policy per environment**: serverless billing tags
come from the policy, so this is what tells QA spend from prod spend.

1. Click your user name › **Settings** › **Compute** › next to **Serverless usage policies**,
   click **Manage** › **Create**.
2. Name it `chatbot-factory-qa` (or `-prod`), select that environment's workspace, and add two
   custom tags: `component` = `chatbot-factory` and `environment` = `qa` (or `prod`) › **Create**.
3. On the policy's **Permissions** tab, use **Grant access** to give **User** to `<jobs_run_as>`,
   the agent service principal and (after B2) the app's service principal.
4. Copy the policy ID from the browser URL when the policy is open. This is
   `<usage_policy_id>`. If you skip this step, set it to `""` in A11; costs then can't be
   attributed to the factory or to an environment.

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

### A11. Store this environment's identifiers [CLI]

The catalog and environment come from the bundle target: `-t qa` uses `qa_chatbot_factory` and
records the environment as `qa`; `-t prod` uses `prod_chatbot_factory` and `prod`. Nothing to
edit for those.

The identifiers that belong to your workspace are **not** stored in the repository. Put them in
a local file per target, which the CLI reads on every `bundle` command and git ignores:

```bash
mkdir -p .databricks/bundle/qa
cp variable-overrides.example.json .databricks/bundle/qa/variable-overrides.json
```

Then edit `.databricks/bundle/qa/variable-overrides.json`:

```json
{
  "warehouse_id": "<warehouse_id from A4>",
  "agent_principal": "<agent_principal from A6>",
  "usage_policy_id": "<usage_policy_id from A9>"
}
```

Do the same under `.databricks/bundle/prod/` with the prod workspace's values. `warehouse_id`
and `agent_principal` are required: every `bundle` command fails with "no value assigned to
required variable" until they are set. Anyone else who deploys needs their own copy of the file.

The two secret scope names (`chatbot-factory`, `chatbot-factory-identity`) are bundle variables
too (`agent_sp_scope`, `identity_scope`). QA and prod are separate workspaces, so each has its
own scopes under the same names and the defaults work for both. Add them to the file only if a
workspace uses different names.

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
databricks bundle validate -t qa
databricks bundle deploy -t qa
```

The first deploy may stop with an error that serving endpoint `chatbot-agent` doesn't exist,
because the app is bound to it and it's created in B3. The jobs are created before that point.
Carry on with B2 and B3, then deploy again in B4.

An error that mentions the app's telemetry tables or `_platform` is a different problem: the
platform schema from A5 step 4 doesn't exist yet, or the deploy identity lacks the permissions
listed there. Fix that and deploy again.

### B2. Create the platform tables [CLI]

```bash
databricks bundle run setup_platform -t qa
```

This creates the platform schema and tables, ABAC column masks, cost views and the shared AI
Search index. It also turns on data classification and anomaly detection, and publishes
settings for the agent. Read the output: any line ending in "not available" is a preview from
A2 that isn't on yet. Turn it on and rerun this step; nothing else is affected.

### B3. Deploy the agent [CLI]

```bash
databricks bundle run deploy_agent -t qa
```

The job prints numbered steps. It first checks the prerequisites from Part A that it can't
build (catalog, SQL warehouse, model endpoints, both secret scopes) and stops with the full
list if any are missing. It then makes sure everything the agent depends on exists, in order,
before deploying it: the control plane tables, the AI Search endpoint and shared index, and the
agent's settings file. On a new workspace the AI Search step waits for the endpoint and index to
finish provisioning, which can take 15 minutes or more.

It then:

- creates the Unity Gateway model services `qa_chatbot_factory._platform.answer_haiku` (Haiku 4.5 with
  a Sonnet 4.5 fallback) and `qa_chatbot_factory._platform.guardrail_evaluator`, and grants **EXECUTE**
  on the answer service to the agent service principal, `mlops` and `<jobs_run_as>` (and on the
  evaluator to `mlops`)
- creates the traces experiment, stored in Unity Catalog
- registers the answer prompt
- in prod only, checks the release gate (Part E)
- deploys the `chatbot-agent` endpoint, tagged with `component`, `environment` and the git commit
- waits until the endpoint is serving the new version, which can take 15 to 20 minutes
- records the release in `platform_releases`: who deployed, the git commit, branch and
  repository, and the configuration it ran with (catalog, workspace, endpoint, warehouse, usage
  policy, a hash of the platform settings)
- removes the agent versions the new one replaced, so the endpoint serves exactly one. Each
  served version keeps compute running, and an endpoint holds at most 15
- registers the production judges, drift monitors, metric views and the Genie space
- prints the Unity Gateway settings that still have to be entered by hand in C2, and warns if
  inference logging isn't set up in this environment's catalog yet. Keep that output open.

### B4. Deploy again and start the app [CLI]

```bash
databricks bundle deploy -t qa
databricks bundle run chatbot_factory -t qa
```

### B5. Give the app its catalog rights [UI]

1. **Compute** › **Apps** › `chatbot-factory` › **Authorization** (or **Overview**). Copy the
   **service principal** Application ID. This is `<app_sp>`.
2. **Catalog** › `qa_chatbot_factory` › **Permissions** › **Grant**, to `<app_sp>`:
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
   databricks bundle run setup_platform -t qa
   ```

### C2. Configure the answer model service [UI]

In the workspace sidebar, click **AI Gateway** › **Models** tab › `qa_chatbot_factory._platform.answer_haiku`.
The exact values are also on **Admin** › **Setup checklist** in the app.

**Permissions**
1. `deploy_agent` grants **EXECUTE** to the agent service principal, the `mlops` group and
   `<jobs_run_as>`. Open the **Permissions** tab and check all three are listed.
2. Only if the job printed "EXECUTE not granted": click **Grant**, select each of the three,
   tick **EXECUTE**, then **Grant**. Without it the agent can't call the model.

**Rate limits**
3. Find the **Rate limits** section (on the service page or under **Edit**). Add:
   - **Service** (whole endpoint): **600** requests per **minute**
   - **Per user** default: **30** requests per **minute**
4. Save. A caller over the limit gets HTTP 429, which the factory logs as an error with the
   reason.

**Inference table**
5. Find **Inference table** (or **Inference logging**). Turn it on with **Catalog**
   `qa_chatbot_factory`, **Schema** `_platform` and **Table prefix** `gw_answer_haiku`. Save.
6. Copy the full table name the page shows, for example
   `qa_chatbot_factory._platform.gw_answer_haiku_payload`. This is `<inference_table>`.

**Guardrail policies.** Open the **Policies** tab › **New policy** and create one policy per row:

| Policy (guardrail) | Phase | Mode | Advanced options › Evaluator | Why |
|---|---|---|---|---|
| **Unsafe Content** (`system.ai.block_unsafe_content`) | Input and Output | **Enforce** | default | Harmful content |
| **Jailbreak** (`system.ai.block_jailbreak`) | Input | **Enforce** | default | Jailbreak and prompt injection |
| **Hallucination** (`system.ai.block_hallucination`) | Output | **Log** (not the default: change it) | `qa_chatbot_factory._platform.guardrail_evaluator` (not the default: change it) | Grounding check. Keep it in Log; see D4 before ever enforcing it |
| **Sensitive data** (`system.ai.detect_sensitive_data`) | Input and Output | **Enforce** (block or redact) | n/a | Select only the restricted categories: Social Security numbers, payment cards, bank account numbers. Leave every other category unselected: names, email addresses, phone numbers, account identifiers, and national health numbers such as UK NHS (which matched ordinary numbers in the documents and replaced them with `[UK_NHS]`). Security decided (2026-10-14) that signed-in people may see business contacts and corporate account identifiers. The input phase also changes the documents the model is given, so a category selected there rewrites the sources before the model reads them |

For each policy:

7. Click **New policy**. Enter a name, for example `jailbreak`.
8. Leave **Applied to** as all principals.
9. Pick the **Guardrail type** from the table.
10. Tick the **Phase** from the table.
11. Choose the **Mode**.
12. For Hallucination only, open **Advanced options** and set the evaluator model service to
    `qa_chatbot_factory._platform.guardrail_evaluator`.
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
    `chatbot-factory` and `environment` = `qa` (or `prod`).

### C3. Configure the guardrail evaluator [UI]

**AI Gateway** › **Models** › `qa_chatbot_factory._platform.guardrail_evaluator`:

1. **Permissions**: `deploy_agent` grants **EXECUTE** to `mlops`; check it's listed. If the
   policy attach step says the evaluator needs query access, also grant it to whoever attached
   the policies.
2. **Inference table** on, with **Catalog** `qa_chatbot_factory`, **Schema** `_platform` and **Table
   prefix** `gw_guardrail_evaluator`. Copy the full table name. This is
   `<evaluator_inference_table>`.
3. **Tags**: `component` = `chatbot-factory` and `environment` = `qa` (or `prod`).
4. No rate limits or policies here.

### C4. Unified trace table [UI] (metastore admin)

1. **AI Gateway** › **Govern** › **Traces** › **Set up tracing**.
2. Choose catalog `qa_chatbot_factory`, schema `_platform` › **Save**.
3. Copy the table name, for example `qa_chatbot_factory._platform.unity_gateway_otel_spans`. This is
   `<trace_table>`.

   Databricks creates the table only when the gateway handles its first call after tracing is
   switched on. If no table is shown, nothing is wrong: finish the rest of the setup, ask a
   chatbot a question (D3), then come back to this screen, or look in Catalog Explorer under
   `qa_chatbot_factory` › `_platform` for the new table, and record its name then.

Do this in each environment's own workspace with that environment's catalog (prod:
`prod_chatbot_factory`), and record the name in that environment's app. If the prod workspace
shows tracing as already set up when you get there, the setting is shared across workspaces on
the same metastore: record the table it shows in prod's app as well instead of creating a second
one. Nothing in the app reads this table; the step only gives `mlops` and `security` access to it.

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
   databricks bundle run deploy_agent -t qa --only observability
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
   QA) › **Save**. The app is stateless, so any instance can serve any user.

### C9. Review queue access [UI]

Review queues live in the traces experiment, on the **Reviews** tab.

1. **Experiments** › `/Shared/chatbot-factory/qa/traces` › **Permissions**. Give each
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
| `SELECT * FROM qa_chatbot_factory._platform.guardrail_events ORDER BY ts DESC` | Rows for the block and refusal, with trace_id |
| `SELECT * FROM qa_chatbot_factory._platform.v_guardrail_verdicts ORDER BY event_time DESC` | Hallucination verdicts for each answer (Log mode) |
| `SELECT * FROM system.ai_gateway.usage WHERE request_tags['bot_id'] = '<bot_id>'` | Tokens per request, tagged with the chatbot |
| **Monitoring** (next day) | Speed, cost, quality and drift charts filling in |

### D4. Decide whether to enforce the hallucination policy (after about two weeks)

Keep it in **Log** unless its record shows it almost never flags good answers. The gateway's
hallucination judge sees only the chatbot's reply, never the documents the answer was built from
(on the output phase Databricks' evaluator judges only that one item). So it can only compare
the answer with the short excerpts quoted in the reply: a claim taken from the rest of the cited
passage, or a citation it can't look up, looks unsupported to it. In QA it blocked two correct,
cited answers that way. The factory checks grounding itself, against the passages actually
retrieved: quotes are verified word for word before an answer is shown, and the groundedness
judges score answers in the quality check and in production.

Compare `v_guardrail_verdicts` (flagged = true) with those groundedness scores and with
thumbs-down feedback. Only if its flags are nearly always right, edit the policy in C2 and change
**Mode** to **Enforce**, and add the change to Admin › Platform (`unity_gateway.policies`, mode
`enforce`) so the two agree. From then on, its blocks are recorded like the others.

---

## Part E: Promote the platform to prod

Prod is its own workspace with its own catalog (`prod_chatbot_factory`), model services,
inference tables, secret scopes and usage policy. Everything in Parts A–D is done again there.

1. In QA, a chatbot has to pass its quality check on the exact commit you're promoting. The
   check runs the chatbot's approved test questions through the deployed agent and judges the
   answers, so it is the end-to-end test of that build:

   ```bash
   databricks bundle run run_evals -t qa --params bot_id=all
   ```

2. Make the QA catalog readable from the prod workspace. The release gate reads
   `qa_chatbot_factory._platform.eval_runs` and `bots` from the prod deploy job, so both
   workspaces must share a metastore, the catalog must be bound to the prod workspace (read-only
   is enough), and the prod deploy identity needs `USE CATALOG`, `USE SCHEMA` and `SELECT` on
   those two tables.
3. In the prod workspace, repeat Part A (catalog `prod_chatbot_factory`, its own usage policy tagged
   `environment` = `prod`, and `.databricks/bundle/prod/variable-overrides.json`), then Parts B–D
   with `-t prod`, using a service principal as the deploy identity. Deploy the same git commit.
4. `deploy_agent` in prod stops at the release gate unless, for this exact platform version and
   git commit:
   - every live QA chatbot passed its latest quality check, and
   - at least one QA chatbot passed. An empty or untested QA doesn't count as a pass.

   It also stops if the git commit is unknown (deploy from a git checkout) or the QA catalog
   can't be read. The message lists every reason.

---

## Moving an existing `dev` deployment to the `qa` target

The bundle target that used to be `dev` is now `qa`. A deployment made with `-t dev` is tracked
under the old name, so deploying with `-t qa` would try to create the app and jobs a second
time. Do this once, in the QA workspace:

1. **Before** updating your checkout (while it still has the `dev` target), release the app
   from the old deployment and remove the old jobs. The app has to survive, because its
   service principal owns the platform schema and holds the grants from B5 and C1:

   ```bash
   databricks bundle deployment unbind chatbot_factory -t dev
   databricks bundle destroy -t dev
   ```

   `destroy` lists what it will delete. Check that it lists only jobs, not the app. It doesn't
   touch the catalog, the agent endpoint, the model services or the experiment.
2. In the workspace browser, open **Shared** › `chatbot-factory` and rename the folder `dev` to
   `qa`, so the existing traces experiment keeps being used at its new path.
3. Update the checkout, create `.databricks/bundle/qa/variable-overrides.json` (A11), then adopt
   the existing app and deploy:

   ```bash
   databricks bundle deployment bind chatbot_factory chatbot-factory -t qa
   databricks bundle deploy -t qa
   databricks bundle run deploy_agent -t qa
   ```

From then on the environment is recorded as `qa` in logs, traces, tags and release records.
Rows written earlier keep `dev`.

---

## Part F: Ongoing operations

| When | What | Where |
|---|---|---|
| Daily | Open alerts | Admin › Alerts (also emailed). They close on their own when the checks stop reporting the problem; **Resolve** closes one sooner |
| Once, after the first deploy | Check the warehouse accepts batched writes | Admin › Setup checklist › **Run the check** |
| Weekly | Review queues for each chatbot | Traces experiment › **Reviews** tab |
| Weekly | Costs vs gateway usage | Admin › Costs |
| Monthly | Idle chatbots (flagged after 45 days) | Admin › Reports › archive |
| As needed | Restore, delete, purge chatbots | Admin › Lifecycle (deleted chatbots are purged automatically after 90 days) |
| As needed | Optimize and promote the answer prompt | Admin › Answer prompt (every chatbot is re-checked afterwards) |
| Before expiry | Rotate the agent service principal secret | A6 steps 3–4, then `deploy_agent` |
| After a policy change | Keep the checklist in step | Admin › Setup checklist |
| After any setup step in Parts A to C | See what is left | Admin › Setup checklist. **Done** and **To do** are what the app checked for itself; the steps it can't see (previews, dashboard embedding, the account budget, app scaling) are boxes you tick once done |
| When someone reports an error | Ask for the text under **Show technical details** | It names the page, the error type and the time (UTC); the full trace is in the app's log at that time |

What owners see in the app is written for people who are not engineers: **Home** shows each
chatbot's next step, **Launch** shows the path to launch as a checklist, and errors say what
to do. When a message says "tell MLOps", the fix is on your side: usually a missing grant
(B5), a setup job that hasn't run (B2, C5) or a job that isn't connected to the app.
The app remembers what it reads for 30 seconds; **Refresh** in its menu shows the result of a
background job straight away.

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

- The model service REST create. `deploy_agent` never modifies a service that already exists
  (the API can't update routing yet), so C2's settings survive reruns. To change a service's
  models later, edit its routing in the Unity Gateway UI; the job prints a warning when the
  routing differs from `models.answer_services` in the settings.
- The blocked-reply shape (`databricks_service_policy`) for streaming and non-streaming answers.
- Whether the evaluator inference table's `request_id` matches the answer service's, which
  `v_guardrail_verdicts` joins on.
- The Review Queues, managed sessions and Genie API calls (each logs and carries on if it fails).
- The ABAC `CREATE POLICY` syntax and the system-table column names in `sql/system_views.sql`.
- Section ids now end in a hash of the section's text, so a section whose text changes gets a
  new id and is embedded again, while unchanged sections are left alone. Sections written by an
  earlier version keep their old ids until their document is next read again; nothing needs to
  be migrated.
- Approving several documents in a row should start one "rebuild the test version" run, not one
  per approval. If the Jobs run list shows one per approval, `jobs.list_runs` isn't returning
  run parameters and the de-duplication is quietly off; nothing breaks, it is only slower.
- In the qa target the bundle runs in development mode, which pauses every schedule, so the
  monitor job doesn't run there on its own. The app doesn't depend on it for alerts (it only
  shows the ones still true, and Admin › Alerts closes the rest), but email alerts, budgets and
  expiry notices need the monitor job: run it by hand in qa when you want to test them.
- If a chatbot's email alerts couldn't be set up when it was created, Admin › Alerts shows
  "Email alerts not set up" with **Try setting them up again**, which runs the maintenance job;
  that job also retries them every night.
- Whether **AI Gateway** › **Govern** › **Traces** is set per workspace or once per metastore
  (C4). The guide assumes per workspace; if it turns out to be shared, QA and prod record the
  same trace table.
- Press **Run the quality check** on Launch: within a few seconds the page should show a progress
  bar with "waiting its turn" and then "running". That relies on the app's service principal
  being able to read a run it started (`jobs.get_run`); if the page instead says it "can't read
  how it is going", give the app's service principal **Can view** on the factory's jobs.
- The quality check reading its scores back: `mlflow.search_traces(run_id=...)` in a job needs
  `MLFLOW_TRACING_SQL_WAREHOUSE_ID`, which every job now sets from its `--warehouse_id`
  parameter. The per-chatbot `[factory] auto-ingest <chatbot>` jobs are copies made when each
  chatbot was created, so they don't have that parameter; they use the ID saved in the settings,
  which the first run of any other job saves. If one of them still stops with "SQL warehouse ID
  is required", archive and restore that chatbot under Admin › Lifecycle to remake its copy.
- The quality check's pace. It asks 4 questions at a time, runs 2 judges per question and makes
  at most 2 judge calls a second (`quality.check` in Admin › Platform, YAML). With MLflow's own
  defaults (10 × 10, no limit) every judge call timed out after 300 seconds against the
  pay-per-token judge endpoint. If the check now finishes comfortably, raise the numbers a step
  at a time; if judges still time out, lower `judge_calls_per_second` or give the judge model a
  provisioned-throughput endpoint. Before it starts, the check asks the judge model one trivial
  question and stops with the reason if that fails.
- If a quality check stalls (scorers timing out, including the plain-Python `checks` scorer), run
  the diagnostic: Admin › Setup checklist › **Diagnose the quality check**, or

  ```bash
  databricks bundle run run_evals -t qa --params bot_id=<bot_id>,channel=candidate,trigger=diagnose,limit=3,diagnose=true
  ```

  It runs three questions one at a time and prints a `DIAG:` line for each thing the check depends
  on, each with its time, `FAILED` and the reason, or `STILL WAITING`:

  | Line | What it shows |
  |---|---|
  | 0 | Who the job calls the chatbot as, and whether the settings name that identity as one of the platform's own callers. |
  | 1, 1b | The chatbot answers one question, and what it did with it (`answered`, `denied`, `error`...). |
  | 1c | The chatbot's **own** trace, written by the serving endpoint under the agent service principal, is read back by its id (it is given 90 seconds to arrive). This is the proof that the endpoint's traces are saved; lines 2 to 6 only prove it for traces the job writes. |
  | 2 to 6 | A trace written by the job: written, sent to storage, read back by id, its steps read, recent traces searched through the SQL warehouse. |
  | 7 | The judge model answers one question. |
  | 8 | The check itself. |
  | 9 | For every question: the chatbot's outcome, how each scorer went (`done`, `failed`, `skipped`, `running`), every score, and `NO SCORE from <scorer>: <reason>` for each one that gave none. Then what trace storage holds for that question, read back by id, and whether the chatbot's own trace for it is stored. |
  | 10 | What searching trace storage for the run finds (what MLflow's evaluation page relies on), each counted on its own: traces found, traces whose request could be read as a question, this check's questions among those, and how many have scores. Traces that are there but can't be decoded are reported as that, never as "0 questions". |

  Every 90 seconds it is still going it also prints what every thread is doing (`Thread 0x...`
  blocks in the run's output), which shows the exact call a stalled scorer is waiting on. It
  records no quality result.
- Where a quality check gets its scores. The scorers run inside the job, so the job keeps what
  each one returns for each question and decides from that. It no longer reads the scores back
  from trace storage to decide, so a slow or failed read-back can't turn judged answers into "0
  questions scored". The scores are still attached to the traces for MLflow's evaluation page.
  A scorer that gave no score is named with its reason in the run's output ("scorers that gave no
  score") and in the stored result; a judge the check requires fails it with that reason.
  `expectations_guidelines` only runs on "should refuse" questions, the only ones that carry
  guidelines of their own; on every other question MLflow's judge raised "Guidelines must be
  specified", which is what "'expectations_guidelines': N/N failed" was.
- How the agent learns who is asking, and what happens when it can't. Model Serving passes the
  agent the credentials of whoever queried the endpoint (on-behalf-of-user authorisation), and
  the agent asks Databricks who that is. This needs the preview in A2 switched on, and the agent
  deployed by `deploy_agent` after that (the deploy declares the user authorisation; nothing in
  the jobs or their identity is involved). If the credentials aren't there, the agent does **not**
  fall back to its own service principal, which is what it used to do: every request then looked
  as if the agent itself had asked, and access was decided for the wrong identity. It now answers
  nobody: people see "I can't confirm who is asking", the request is logged as an error of type
  `CallerUnknown`, and a quality check or the diagnostic (line 1b) stops with the reason and the
  three things to check. The reason ends with the kind of failure in brackets: `ValueError` means
  no user token arrived at all; anything else means a token arrived but Databricks would not say
  whose it is (for example the token's scopes don't allow the lookup). It also refuses
  credentials that turn out to be the agent's own. If the
  preview is on and the agent was redeployed and line 1b still says the agent could not get the
  asking user's credentials, open a Databricks support case with: the workspace, the endpoint
  name, the time of the request, the `request_id` from the run's output, the model version the
  endpoint serves, that the model was logged with a `UserAuthPolicy` (`sql.statement-execution`,
  `sql.warehouses`) and creates its client inside `predict` with
  `ModelServingUserCredentials`, and the `databricks-ai-bridge` warning from the endpoint's logs
  ("Unable to get auth from Databricks Model Serving Environment").
- How the agent signs in to AI Search. Searching is shared across chatbots and runs as the agent
  service principal, with the same secret-backed credentials the deploy puts on the endpoint
  (`serving.agent_search_credentials`). Left to itself the AI Search client borrowed MLflow's
  credentials, which carry no token once the endpoint signs in with OAuth client credentials, and
  it refused to start: "Please specify either personal access token or service principal client
  ID and secret." Who may use a chatbot is still decided first, from the asking user's own
  credentials, and the search is filtered to that chatbot. The deploy prints "Search index
  grants" with what succeeded and what failed (and stops if any failed), then whether the agent
  service principal may use the AI Search endpoint. If a chatbot then reports a permission error
  while searching, give that principal **Can Use** on the endpoint's **Permissions** in the UI.
- The answer contract (`factory/answer_contract.py`). The answer model is held to a strict JSON
  schema: the request carries `response_format` (`json_schema`, strict) and is sent without
  streaming, because Claude models on Databricks take structured output only without streaming.
  So every route of the answer model service must be a model that supports structured output
  (Databricks lists Claude Opus 5.5, Sonnet 5.5, Haiku 5.5 and Fable 5.1 as not supporting
  it): check the fallback route too before relying on it. Answers are not streamed, so `ttft_ms`
  stays empty for them. The reply is checked in this order: a gateway denial, the finish reason
  (cut off at the length limit is a truncation), valid JSON, the schema (statuses, field types,
  evidence structure, what each status requires), then the evidence. The model answers in
  blocks, each with the SOURCE and numbered lines behind it (a sentence, or one table row);
  lines need not be next to each other, a run longer than four lines is split into several
  excerpts, and the budget is 12 lines per block and 30 per answer. The agent cuts the excerpts
  out of the source, writes the [n] markers itself, and checks every quotation, including short
  ones such as a button label or an amount, against the lines its own block cites, so a quote
  from one source can't be credited to another. What will be shown is checked once more after
  redaction. A shortened excerpt is stored as the exact words kept, with `truncated` set; the
  "…" is added only where it is shown. A reply that fails is sent back once, with the problems
  by block and source line added to the system message (never the earlier reply itself), and
  withheld if it fails again. Each failure is recorded as one of `invalid_json`,
  `schema_validation`, `truncation`, `evidence_failure`, or a gateway `policy_denial` (outcome
  blocked); a failed request is not sent again automatically.
- Why answers were withheld. When an answer still fails its checks, the platform's own callers
  are told why (the kinds of problem and the problem text). The quality check prints each
  question's reasons in the diagnostic, a count by cause in every run ("Withheld because of:
  …"), and stores it with the result. Launch then leads with how many questions were answered
  and what happened to the rest, and shows the scores over the answers given beside the scores
  that decide the gate. The gate itself still counts every question.
- Questions near a platform rule. Decided 2026-10-14: asked, for example, whether an injury is
  covered by workers' comp, the chatbot may say what the documents state and who decides (or
  decline), but never make the decision, decide the person's own case or give the advice. The
  platform rules and the answer prompt say so, and such test questions are a third kind, **Share
  documents, don't decide**, judged by `rule_compliance` (target 100%) against the chatbot's
  platform rules instead of by refusal accuracy. "Should refuse" is now only for questions
  outside the purpose or on a topic the owner asked it to decline. The generator writes 6 of
  those and 4 rule questions for a chatbot that has none of each. Chatbots made before this have
  rule questions filed under "Should refuse": on **Test questions**, change their **Type** to
  "Share documents, don't decide".
- What each question is scored on. Answer-quality judges (correctness, groundedness, relevance,
  the retrieval judges) run only on answerable questions; an expected refusal is scored on
  refusal accuracy and safety, a rule question on its rule rubric (`rule_compliance`) and
  safety. Before, a correct refusal got groundedness 0, so five correct refusals alone could
  keep groundedness under its 90%. On an answerable question, anything but an answer (blocked,
  refused, no source, withheld) counts 0 on every answer-quality score and in `answer_rate`
  (target 95%), and the check's failure line lists those outcomes by kind. The platform-rules
  judge uses the same rubric as rule questions (declining, or sharing what the documents say and
  who decides, follows the rules). Every percentage on Launch shows what it is out of, for
  example "3 of 5 answerable questions"; "Rule questions handled correctly" counts only rule
  questions, "Platform rules followed (all questions)" every question.
- Running the check on chosen questions only. Pass their eval_ids (shown on each question's
  diagnostic line and stored with every result), separated by semicolons or spaces:

  ```bash
  databricks bundle run run_evals -t qa --params "bot_id=<bot_id>,channel=candidate,trigger=targeted,eval_ids=<id1>;<id2>"
  ```

  Like a run with `limit`, it records no quality result for the release.
- Test questions follow their documents. When a document is archived, flagged, deleted, replaced
  by a new version, or has its text or pages changed by hand, every approved question whose
  source is that document loses its approval and gets a note saying why ("Needs review because"
  on Test questions); the audit log records which questions and why, and the table's history
  keeps every earlier version. The owner checks the expected answer against a current document,
  sets the question's **Source document** (documents with the same name are told apart by
  their id; archived or missing ones are labelled), and approves it again, which clears the
  note. Before anything is asked or scored, the quality check stops if any approved question
  points at a document that is missing or not in use, and lists them. Generated questions name
  the system or program they are about (myCigna, Workday) and ask about current policy, not a
  worked example, a screenshot's sample value or a past deadline.
- Quality check bookkeeping. Results are kept by each test question's `eval_id`, not its text,
  so a question that appears twice keeps two records. The correctness judge runs only on
  questions with an expected answer or expected facts; "should refuse" questions are judged by
  refusal accuracy instead.
- Answers blocked by a gateway guardrail. The diagnostic names the guardrail and the stage on
  line 1b and for each blocked question on line 9 (only the platform's own callers are told
  which guardrail it was), and line 9 counts every outcome ("answered 1, blocked by a guardrail
  2") instead of only the ones that were faults. A block from a policy that the settings put in
  Log mode is marked SETTINGS MISMATCH and recorded as a `gateway_policy_mode` event: a policy
  in Log mode never blocks, so its live mode in the Unity Gateway UI is Enforce. That is what the
  QA run hit: the hallucination policy had been created with the UI's defaults (Enforce, default
  evaluator) instead of C2's Log with `guardrail_evaluator`. Fix it on the answer service's
  **Policies** tab; see D4 for why it should stay in Log.
- Trace requests read back from storage. A trace's request can come back as JSON text of JSON
  text (encoded twice). Everything that reads span or trace values goes through one decoder
  (`tracing.as_dict`), which decodes again while the value is still text, four layers at most,
  and returns an empty dictionary for anything else.
- Who gets past a chatbot's access list. A request does only when it says it is a test question
  (`synthetic`) **and** its caller is one of the platform's own: the identity in
  `access.jobs_run_as`, or one listed in `access.synthetic_callers` or `access.trusted_callers`.
  The agent, the quality check and the diagnostic all use that one rule. Everyone else is checked
  against the chatbot's access list as usual, whatever the request says. So `access.jobs_run_as`
  (C1) must be the identity the jobs really run as: in QA, where the bundle is deployed in
  development mode, that is the person who deployed it. A check whose first question is refused
  stops with "there is nothing to score", names the identity it called as and says which setting
  to change. The agent picks a changed setting up within a minute; the rule itself needs the
  agent redeployed (`deploy_agent`) once after this change.
- Who the agent's endpoint signs in as. The deploy gives the endpoint the agent service
  principal's credentials (A6) as references to the secret scope, together with this workspace's
  own host and `DATABRICKS_AUTH_TYPE=oauth-m2m`. The host matters: without it the SDK skips
  that sign-in and uses the one built into Model Serving, an implicit identity nobody chose and
  nothing was granted to, and the chatbot then can't read its settings or store its traces. The
  agent service principal is therefore the trace writer: the deploy and the observability task
  grant it the catalog, the schema and the four `traces_otel_*` tables, print how many grants
  succeeded and list any that failed with the reason. Nothing has to be looked up or pasted;
  Admin shows the identity under Platform.
- A chatbot that answers with "Sorry, something went wrong". The quality check now asks one
  question first and stops with the reason, instead of scoring error messages. The agent gives
  the reason only to the platform's own callers (the rule above), so the jobs' identity must be
  recorded in `access.jobs_run_as` (C1), and the agent must have been redeployed
  (`deploy_agent`) since this change.
- Redeploying the agent after a new commit. The endpoint carries a `git_commit` tag, and
  `agents.deploy` refuses to change a tag's value in one request ("Duplicate key(s) detected in
  tags to add and tags to delete"). The deploy therefore hands over existing tags unchanged and
  sets their new values once the endpoint is serving; the run prints "Endpoint tags updated".
- README › "Verify against a live workspace first" has the full list.
