# Chatbot Factory: UI requirements

Paste this into Claude Design. It describes every screen, what's on it, and what each action does.

## Product

An internal web app where non-technical employees build their own document chatbots in
minutes. Owners name the bot, answer a few questions, upload documents, check that the
documents were read correctly, approve test questions, and send the bot for approval. A
Reviewer approves it and it goes live. MLOps admins manage platform settings.

**Users:** Owner (creates and runs a bot), Reviewer (approves releases), Tester (up to 10 per
bot, tries the test version), End user (chats), Admin (MLOps).

**Tone:** Plain English, no jargon. Never show words like "index", "chunk", "embedding",
"schema", "MRR" or "candidate". Say "test version", "live version", "the chatbot found the
right document first for 92% of questions". Friendly and confident, like a good consumer app.

## Visual direction

- Clean, calm enterprise look: white and very light gray surfaces, one accent color, generous
  spacing, 8px radius cards, subtle borders instead of heavy shadows.
- Status colors only: green (good/live), amber (needs attention/test), red (blocked/failed),
  gray (draft/archived). Always pair color with an icon and a word.
- Desktop first (1280–1440 wide), responsive down to tablet. Chat must work on phone.
- Left sidebar navigation: Home, Create chatbot, Documents, Test questions, Launch, Monitoring,
  Traces, Chat, then All chatbots and Admin (admins only). A chatbot picker at the top of the sidebar sets the current bot for
  Documents, Test questions and Launch.

## Shared components

- **Status pill:** Draft · Setting up · Setup failed · Testing · Waiting for approval · Live ·
  Paused · Paused (budget reached) · Archived.
- **Version chips:** "Live v7" (green) and "Test v8" (amber).
- **Readability badge** per document: Good (green) · Check it (amber) · Problem (red) · n/a for text files.
- **Progress checklist** for long jobs: step name, spinner/check/cross, plain-English error and a "Try again" button.
- **Metric tile:** big percentage, label in plain words, small "needs 90%" target, pass/fail icon.
- **Empty states** with one clear next action on every list.
- **Toasts** for saves; **confirm dialogs** for archive, rollback, approve.
- **Explainer (i) icon** next to every step, option and chart: on hover or keyboard focus it
  explains in plain English what this does for the person, names the Databricks feature doing
  it, and links to that feature's docs (for example, the prompt optimizer says we rewrite the
  instructions automatically with MLflow prompt optimization (GEPA) and the Prompt Registry).
  Texts live in `src/factory/explain.py`; the app shows them as `help=` tooltips.

## Screens

### 1. Home
- Greeting and a primary "Create a chatbot" button. Owners see no costs or budgets anywhere (MLOps manages them).
- Cards for each chatbot the user owns, reviews or tests: name, purpose (one line), status
  pill, version chips, questions this week, thumbs-up rate, month spend vs budget bar
  ($312 of $1,000), and a "needs you" badge (e.g. "3 documents to review", "Waiting for your
  approval", "Document expires in 12 days").
- "Needs your attention" list at top: approvals waiting, expiring documents, failed checks,
  budget at 70/90/100%.

### 2. Create chatbot: fast path or advanced mode
- First screen: two cards. **Fast path** (recommended, about 3 minutes) asks only what can't be
  decided for the owner: name and purpose, who approves new versions (plus the owner group,
  prefilled from the person's groups), who can use it, and the documents. A "What we set for you"
  panel lists the defaults (short answers with sources, conversation memory, named logs, short
  quotes, $1,000 budget with alerts, test questions written for you, internal documents valid
  until replaced). **Advanced mode** is the six steps below. Either can switch to the other.

**Advanced mode** (6 steps, progress bar whose steps can be clicked, Back/Next, autosaves as draft)
1. **Name and purpose:** chatbot name (shows the technical name live underneath and warns
   about duplicates), what it helps with (1–2 sentences), business function, team.
2. **People:** owner (work email, defaults to me), owner group, Reviewer (a group, or a person
   by work email), testers (up to 10 work emails, counter "3 of 10").
   - **Identity inputs everywhere:** people are always entered as **work emails** (checked
     against Databricks users as you type, with a plain error if not found); groups are always
     **picked from a searchable list** of directory groups, the person's own groups first, with
     an (i) explaining where groups come from and who to ask. Never free-text group names.
3. **Who can use it:** "People sign in themselves (the chat page, also as a Microsoft Teams
   tab)" with a choice of **Everyone at the company** (anyone who can sign in with their company
   account; the built-in `account users` group) or specific groups (picker) plus optional
   individual emails, or "Another app asks on their
   behalf (a Teams bot, a portal, a CRM)" (only Public documents allowed; explain in one line).
   The (i) explains that a Teams tab showing the chat page counts as people signing in.
   Channels: Chat page, API.
4. **Documents:** sensitivity (Public / Internal / Confidential with one-line explanations),
   source (upload files / git repository of markdown). After upload, a row per file with
   **its own** expiry: "Valid until replaced" (default) or an expiry date. No "how often do they
   change" question: new files are read automatically.
5. **Answers and safety:**
   - Answer style (short / detailed).
   - **Rules for every chatbot** (read-only, locked icon, "Set by MLOps"): do not give legal,
     medical, individual tax, investment or policy-coverage advice; do not explain how to make
     weapons, help with malicious cybersecurity, produce controlled substances or commit
     crimes; do not make high-stakes decisions; do not present itself as an authority.
     Owners cannot turn these off. No toggles for keyword blocking, advice disclaimers or
     competitor names.
   - Built-in protections (read-only list): harmful content filter, personal data masking,
     jailbreak blocking, hidden-instruction removal, exact source quotes, "I don't know" when
     documents don't say, conflict disclosure, no invented links.
   - **Add your own topics to refuse** (optional): an empty chip input. Nothing is
     pre-filled; owners can only add.
   - "Keep quoted text short" toggle (forced on, with the reason, for "Through another app").
   - Conversation memory: Off / This conversation only / Saved. Identity in logs: Named /
     Pseudonymous (Saved memory disabled when Pseudonymous, with the reason shown).
6. **Alerts and review:** no budget or cost questions: MLOps sets each chatbot's budget in Admin ›
   Chatbots, budget alerts go to MLOps only, and owners never see costs (Home cards, Monitoring and
   the wizard show none). Alerts: required ones listed as always on (outages, slowness, errors,
   expiry); optional "Unusual traffic" toggle. Then a summary of all answers with Edit links and a
   big **Create chatbot** button.
- After Go: progress checklist (Creating workspace → Reading documents → Checking
  quality → Writing test questions → Choosing the best search method → First quality check),
  then lands on Documents.

### 3. Documents
- Upload area (drag and drop; PDF, Word, PowerPoint, images, markdown, text; max 100 MB /
  500 pages). Per upload: effective date, expiry date or "until replaced", page range
  (optional). Duplicate file → "Already uploaded" message. Same name → "Saved as version 3".
- Table: name, version, readability badge, status (Needs review / Approved / Flagged /
  Blocked: sensitive data / Archived), expires, owner, in test version (yes/no), in live (yes/no).
  Filters by status; search.
- **Review drawer/page** for one document: left = page image, right = extracted text for
  that page with its passages; page navigator; plain-English issues ("We couldn't read page
  4", "A table on page 2 may be scrambled"); buttons Approve, Flag passage, Re-read with
  different settings. Tabs: Review · Details (edit owner, dates, scopes without re-reading) ·
  History (versions, who approved, when).
- Actions: Archive (not delete), Restore, Download original, Upload new version.
- Banner when changes are waiting: "You have changes in the test version. Check and send for
  approval on the Launch page."

### 4. Test questions
- Tip banner: "Add a few questions of your own, the way real users would ask them."
- Editable table: question, expected answer, type (Should answer / Should refuse / From user
  feedback), difficulty (Easy / Hard), source document and page, approved checkbox, and an
  **Edit** button next to Delete on every row that opens the row for editing (question, expected
  answer, type) with Save and Cancel. A hint says cells can also be edited in place.
- Add row inline. Buttons: Suggest more questions, Run a check now.
- Counter and progress bar: "38 of 50 approved". **50 approved questions are required** to
  send for approval; the button stays disabled with that reason until then. Suggested
  questions are generated with ~30% extra so 50 survive review.

### 5. Launch (the release screen)
- Header: status pill, Live version chip, Test version chip.
- **Test version card:**
  - What changed since live (documents added/removed/updated, settings changed).
  - "How we search your documents": one sentence ("We tested 5 ways… 'Smart search with
    re-ranking' worked best: right document first for 92% of questions.").
  - Quality check: metric tiles in two groups. *Finding the right document:* first result,
    top 3, top 5, overall ranking. *Answer quality:* correct, sticks to sources, relevant,
    citations accurate, refuses correctly, safe. Each tile shows score and target.
    Hard failures listed in red ("1 answer cited the wrong page; this must be zero").
  - Safety review (Reviewer only, when flagged): list of flagged answers, required reason box,
    "Override and re-check".
  - Testers panel: chips, "3 of 10", add/remove.
  - Primary actions by role/state: Owner "Send for approval" (disabled with reason until the
    check passes); Reviewer "Approve and publish" / "Send back for changes" with comment.
  - After approve: progress (Publishing → Updating search → Smoke test) and outcome; if the
    smoke test fails: "We kept the previous version live" with details.
- **Live version card:** published date, approved by, document count, API snippet (copy
  button) when API is enabled, Pause/Resume, "Roll back to previous version" (confirm dialog
  naming the version it returns to).
- **Release history** timeline: version, date, approved by, check result, smoke test, rolled back?
- **Documentation:** auto-generated model card preview with Download.
- Archive chatbot (danger zone, confirm).

### 6. Chat
- Chatbot picker; for owners/Reviewer/testers a toggle "Live / Test version" with an amber
  test banner.
- Message list. Assistant messages show:
  - Small outcome label when not a normal answer: "Not covered by my documents", "Outside
    what I can help with", "No access", "Couldn't verify, showing sources".
  - Inline citation markers [1] [2] that highlight the matching source.
  - "Sources" section: document name, version, page(s), section, and the exact quoted
    excerpt in a quote block; link to open the document at that page.
  - Amber "Sources disagree" callout when documents conflict.
  - Thumbs up / down, with optional comment on down.
- "New conversation" button. Memory indicator: "Remembers this conversation" / "Doesn't
  remember earlier messages".

### 7. Admin (MLOps only)
- Tabs:
  - **Platform:** grouped forms for quality targets, models, pricing, monitoring thresholds
    (latency 5/12/20 s, error rate 5%, traffic window), retention (7 years), trusted
    middleware apps, synthetic callers, groups, and the **platform rules** list (edit text,
    add, remove for everyone). Save triggers a re-check of all chatbots.
  - **Chatbots:** table of all bots with state, owner, spend, critical flag, answer
    model, query rewrite toggle, platform pin, and "Platform rules turned off for this chatbot"
    (multi-select; admin only, recorded in the audit log and model card).
  - **User access:** CSV upload with preview and row count.
  - **Alerts:** open alerts with severity, chatbot, message, Resolve.
  - **Audit log:** searchable, filter by chatbot/actor/action.
  - **Re-index** after a reader change.

### 8. Monitoring (owners, Reviewer, admins)
- Period picker (7 / 30 / 90 days); links to "Open traces in MLflow" (filtered to this chatbot)
  and the Databricks drift dashboard.
- Headline tiles for the last 7 days: questions, typical and slow (p95) answer time, time to
  first token (p95), cost per question, answered share.
- Tabs with line charts: **Speed** (answer time, first token, share rewritten by guardrails);
  **Cost and tokens**; **Quality** (outcome mix, judge pass rates, thumbs up); **Guardrails**
  (Unity Gateway Log-mode checks vs flagged, every guardrail that fired per day by rule in plain
  words, and a table of recent events with trace IDs; never question text); **Retrieval**
  (best search score, results moved by the reranker, per-field drift table with PSI);
  **Question drift** (drift vs launch and vs last week with a status pill, and a 2-D question map
  colored by week or outcome; dots only, never question text). The map has an (i) and a "How to
  read this" line: each dot is a question, close dots are similar questions, groups are topics;
  look for "not in documents" groups (add a document) and groups new in recent weeks (new
  topics). Hovering a dot shows its topic name (a short, people-safe name written by the LLM),
  week and outcome.
- **Quality tab:** judge pass rates with the go-live target as a dashed line and red markers on days
  below it; **Recurring problems** (judge failures grouped by judge and topic, with trend). Speed
  and drift charts also show their target or alert line with red markers where it was crossed.
- Links: the chatbot's **Review queue** in MLflow, traces, Genie.
- **Every chart shows values on hover:** bars show their value; line charts show the date and
  every series' value for the nearest day, with a guide line.

### 9. Admin additions (MLOps)
- **Platform:** a form for everyday settings (go-live targets, monitoring targets, models and prices,
  groups and trusted apps, retention, Unity Gateway table names), and an "All settings (YAML)"
  expander for everything else.
- **Chatbots:** a chatbot picker at the top (searchable dropdown), then per-chatbot judge switches (built-in judges on by default; custom judge with
  instructions, conversation judges and simulated conversations off), observability switches
  (version linking, issue detection, custom trace view).
- **Lifecycle:** archived, deleted and purged chatbots with Restore, Delete, and "Purge now"
  (type the technical name to confirm); days left before automatic purge.
- **Reports:** every chatbot with state, owner, last question, questions, cost, answered rate,
  p95 answer time and first token, judge pass rate, open alerts; period picker; CSV download;
  idle chatbots with an Archive button; "Run maintenance now".
- **Costs:** estimate vs gateway-recorded tokens per chatbot, warehouse work per chatbot,
  billed spend by tag.
- **Answer prompt:** optimization history, "Optimize", "Promote @optimized to production".
- **Setup checklist:** service policies to attach per model service (phase, Enforce/Log), and
  other UI-only Databricks steps with done/not-done marks.
- Monitoring page: links to the chatbot's MLflow traces and the Genie agent, and the embedded
  Databricks drift dashboard.

### 10. Traces (owners see the typical flow; MLOps and Security also see single traces)
- **Typical flow:** every step an answer goes through, in order and indented like a tree
  (agent, search, rerank, prompt, AI model call, guardrail checks, rendering), each with its
  typical time, slow (p95) time, how often it runs and its cost per question. Selecting a step
  shows what it does in plain English, the Databricks feature behind it, a histogram of how
  long it takes, the **slowest runs of that step** (MLOps and Security can open each as a trace),
  and the exact code that runs it.
- **One trace:** search recent questions, pick one; summary of question, outcome, total time,
  time to first token, tokens in/out, cost, rewrites, release and prompt version. The same tree
  with that trace's times; selecting a step shows its inputs and outputs in full: the messages
  sent to the AI (system prompt, sources, question), the raw model output, search results with
  rank before and after reranking and scores, guardrail results, and every recorded attribute.

### 11. All chatbots (MLOps)
- Filter: all chatbots or one; period 7 / 30 / 90 days.
- Tiles: questions, cost, answered, judges pass, slow answers, first token, guardrail events,
  open alerts.
- Charts: questions per day by chatbot; cost per day; time per step (typical vs slow, from
  traces); slow answers by weekday and hour (heatmap).
- **Question topics, worst first:** similar questions grouped daily over 28 days, each with a short
  people-safe name written by the LLM, with questions, trend, answered, not in documents, judges pass, best search
  score and slow time.
- **Compare slices:** a measure (judges pass, answered, cost per question, slow time) split by two
  dimensions (chatbot, prompt version, release, model, caller, business function), each cell with
  its count.
- **Gaps in the documents:** distribution of best search scores and the lowest-scoring
  unanswered questions.
- **Conversations:** questions per conversation, asked again after a miss, left after a miss.
- **Recurring problems:** judge failures grouped by chatbot, judge and topic with trend and the latest
  judge reason; selecting one opens an example trace.
- Selecting a topic lists its questions; selecting a question (topics or document gaps) opens it on
  Traces › One trace. Gaps have **Send these to each chatbot's review queue**. Link to Genie.
- **Guardrails** across all chatbots.
- **Every chatbot:** searchable, paginated (25 per page) table with status, owner, questions,
  answered, slow time and cost; CSV download.

### Click-through details (built for guardrail bars and the slow-answer heatmap; extend as needed)
- **Drill into any data point:** clicking a bar, point or table row opens a popup listing the
  events behind it (for example every "Off-topic refused" event: time, chatbot, reason, trace
  link) with filters, a link to each trace, and **Download CSV**. A downloadable report is the
  fallback where a popup doesn't fit.

## States to design

Loading skeletons, empty lists, setup failed with retry, check running, check failed,
waiting for approval (owner view vs Reviewer view), live with test changes pending, paused
(budget), archived (read-only), no access.

## Data and actions (for developers)

| UI action | Backend |
|---|---|
| Go (wizard) | `ControlPlane.save_config` → provision job |
| Upload / new version / archive / restore / metadata edit / download | `Documents.register / archive / restore / update_metadata / download` → ingest job |
| Approve / flag document | `Documents.approve / flag` → ingest job (publish only) |
| Save test questions | `golden_set` table → ingest job (publish only) |
| Run a check | `run_evals` job, channel=candidate |
| Send for approval / send back | `releases.review_requested_at`, `set_state` |
| Approve and publish / roll back | `promote` job (action promote / rollback) |
| Pause / resume / archive | `ControlPlane.set_state` |
| Chat | serving endpoint `responses.create` with `custom_inputs` {bot_id, channel, conversation_id}; render `custom_outputs` {outcome, answer, citations[{n, doc_name, doc_version, pages, section, excerpt}], conflicts} |
| Thumbs | `feedback` table |
| Admin save | `ControlPlane.set_setting` → evals job for all bots |
