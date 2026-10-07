"""Plain-English explainers behind every (i) icon (UX-2): what the step or option does for the
person, the Databricks feature doing it, and a link to that feature's docs. One source of truth
for the app's help tooltips and the design canvas."""

DOCS = "https://docs.databricks.com/aws/en"

# key: (what it does for you, Databricks feature, docs link)
EXPLAIN: dict[str, tuple[str, str, str]] = {
    "fast_path": ("Answer four questions and upload your documents. We pick proven settings for everything "
                  "else, and you can change any of them later.", "", ""),
    "advanced_mode": ("Every setting, one step at a time: who can use it, answer style, memory, privacy, "
                      "quoting and alerts. Pick this if you have specific requirements.", "", ""),
    "purpose": ("Your one-sentence purpose becomes part of the chatbot's instructions and decides which "
                "questions it answers and which it politely declines.", "MLflow Prompt Registry",
                f"{DOCS}/mlflow3/genai/prompt-version-mgmt/prompt-registry/"),
    "people": ("The owner runs the chatbot day to day. The Reviewer approves every new version before anyone "
               "else sees it, so nothing changes without a second pair of eyes.", "Unity Catalog permissions",
               f"{DOCS}/data-governance/unity-catalog/manage-privileges/"),
    "access": ("Only these groups can ask questions. Before every answer we check the person's access in "
               "Unity Catalog, so removing someone from the group takes effect within minutes.",
               "Unity Catalog permissions", f"{DOCS}/data-governance/unity-catalog/manage-privileges/"),
    "documents": ("We read each file (including tables, scans and slides), split it into sections and index "
                  "it for search. Uploading a new file starts this automatically.",
                  "ai_parse_document, AI Search and file arrival triggers",
                  f"{DOCS}/sql/language-manual/functions/ai_parse_document"),
    "search": ("We try five ways of searching your documents with your test questions and keep the one that "
               "finds the right section most often, including a reranker that double-checks the top results.",
               "Databricks AI Search", f"{DOCS}/vector-search/vector-search"),
    "rules": ("These rules apply to every chatbot and only MLOps can change them. They stop the chatbot from "
              "giving advice or making decisions it shouldn't.", "Unity Gateway service policies",
              f"{DOCS}/data-governance/unity-catalog/service-policies/"),
    "guardrails": ("Every question and answer passes safety checks: harmful content, jailbreak attempts, "
                   "personal data and answers not backed by your documents. People only see a polite "
                   "message; every check is recorded.", "Unity Gateway service policies",
                   f"{DOCS}/data-governance/unity-catalog/service-policies/"),
    "refuse": ("Add any extra topics this chatbot should decline, on top of the platform rules.",
               "Chatbot Factory guardrails", f"{DOCS}/data-governance/unity-catalog/service-policies/"),
    "quotes": ("Caps how much text is quoted word for word, so whole documents can never be copied out "
               "through the chatbot.", "Chatbot Factory guardrails",
               f"{DOCS}/data-governance/unity-catalog/service-policies/"),
    "identity": ("Named logs show who asked. Pseudonymous logs show a code instead; only Security can look up "
                 "the person, with a key kept in a locked secret scope.", "Databricks secrets",
                 f"{DOCS}/security/secrets/"),
    "memory": ("Lets people ask follow-up questions like \"what about for property?\". Saved conversations "
               "are kept server-side so people can come back to them.", "Agent memory (managed sessions)",
               f"{DOCS}/machine-learning/model-serving/"),
    "groups": ("Groups come from your company directory (Entra ID or Okta) and are synced into Databricks. "
               "Pick one you're in, or search all groups. Not sure which? Ask your manager or IT which group "
               "covers the people who need this; using a group means joiners and leavers are handled for you.",
               "Unity Catalog groups", f"{DOCS}/admin/users-groups/groups"),
    "people_ids": ("Use the person's work email, exactly as they sign in to Databricks. We check it exists as you "
                   "type.", "Databricks users", f"{DOCS}/admin/users-groups/users"),
    "teams": ("Adding the chat page as a Microsoft Teams tab still counts as people signing in themselves: each "
              "person uses their own Databricks access. A Teams bot or another app that asks on people's behalf "
              "counts as \"another app\".", "Databricks Apps (embedding)", f"{DOCS}/dev-tools/databricks-apps/embed"),
    "expiry": ("Set this per document. Most documents stay valid until you upload a newer version; some, like a "
               "yearly rate sheet, stop being valid on a date. Expired documents stop being used in answers, and "
               "we email the owner a month before.", "AI Search filters", f"{DOCS}/vector-search/vector-search"),
    "question_map": ("Each dot is one question someone asked, placed so similar questions sit close together. "
                     "Groups of dots are topics. Look for clusters colored \"not in documents\" (add a document "
                     "that covers them) and for new clusters that appear in recent weeks (new topics people care "
                     "about). The map never shows question text.", "Data quality monitoring",
                     f"{DOCS}/lakehouse-monitoring/"),
    "everyone": ("\"Everyone at the company\" lets anyone who can sign in to Databricks with their company "
                 "account use the chatbot. MLOps can make every employee a sign-in-only Databricks user (consumer "
                 "access, no data or compute rights) and limit sign-in to the company network.",
                 "Consumer access and IP access lists", f"{DOCS}/security/network/front-end/ip-access-list"),
    "review": ("New versions wait for the Reviewer's approval. After approval we publish, run a smoke test, "
               "and roll back automatically if it fails.", "Model Serving",
               f"{DOCS}/machine-learning/model-serving/"),
    "test_questions": ("50 approved questions are needed before launch. We run all of them after every "
                       "change and score the answers with AI judges, so you know quality didn't slip.",
                       "MLflow evaluation (mlflow.genai.evaluate)", f"{DOCS}/mlflow3/genai/eval-monitor/"),
    "judges": ("AI judges grade answers for correctness, sticking to sources, relevance and safety, on test "
               "questions and on a sample of live answers.", "MLflow built-in LLM judges",
               f"{DOCS}/mlflow3/genai/eval-monitor/concepts/judges/"),
    "prompt_optimizer": ("We rewrite the chatbot's instructions automatically, test each rewrite against your "
                         "approved questions, and keep the best one. Nothing changes until an admin promotes it, "
                         "and every chatbot is re-checked afterwards.", "MLflow prompt optimization (GEPA) and "
                         "Prompt Registry", "https://mlflow.org/docs/latest/genai/prompt-registry/optimize-prompts/"),
    "traces": ("Every question is recorded step by step: what was searched, what the AI was sent, what came "
               "back, how long each step took and what it cost.", "MLflow Tracing (stored in Unity Catalog)",
               f"{DOCS}/mlflow3/genai/tracing/trace-unity-catalog"),
    "flow": ("The steps every question goes through, in order, with how long each usually takes. Click a step "
             "to see what it does, the code behind it, its timing spread and its cost.", "MLflow Tracing",
             f"{DOCS}/mlflow3/genai/tracing/observe-with-traces/ui-traces"),
    "review_queue": ("Answers that failed a judge or got a thumbs down are queued for a person to check. "
                     "Confirmed problems become new test questions.", "MLflow Review Queues",
                     f"{DOCS}/mlflow3/genai/human-feedback/expert-feedback/review-queues"),
    "monitoring": ("Live answers are sampled and scored by the same judges used before launch, and alerts fire "
                   "when quality, speed or cost drift.", "MLflow production monitoring",
                   f"{DOCS}/mlflow3/genai/eval-monitor/production-monitoring"),
    "drift": ("Shows whether the questions people ask are changing compared with launch. Big shifts usually "
              "mean new topics your documents don't cover.", "Data quality monitoring",
              f"{DOCS}/lakehouse-monitoring/"),
    "clusters": ("Groups similar questions into topics and scores each topic, so you can see which kinds of "
                 "questions fail most and are growing.", "Chatbot Factory topic clustering",
                 f"{DOCS}/lakehouse-monitoring/"),
    "issues": ("Answers the judges failed, grouped by which judge failed them and what the question was about, so "
               "a pattern (one topic failing again and again) stands out. Fix it with a document, a test question "
               "or a rule.", "MLflow production monitoring", f"{DOCS}/mlflow3/genai/eval-monitor/production-monitoring"),
    "genie": ("Ask questions about usage, quality and cost in plain English and get charts back.", "Genie",
              f"{DOCS}/genie/"),
    "costs": ("Our estimate from token counts, next to what Databricks recorded and billed, so you can trust "
              "the numbers.", "System tables (ai_gateway.usage, billing.usage)", f"{DOCS}/admin/system-tables/"),
    "masking": ("Personal data is masked in logs for everyone except Security and MLOps, automatically, "
                "based on column tags.", "Unity Catalog ABAC", f"{DOCS}/data-governance/unity-catalog/abac/"),
    "lifecycle": ("Archived chatbots stop answering but keep everything. Deleted ones are purged after 90 days; "
                  "audit history, logs and traces are kept for 7 years.", "Unity Catalog",
                  f"{DOCS}/data-governance/unity-catalog/manage-privileges/"),
}


def help_text(key: str) -> str:
    """Markdown for a Streamlit `help=` tooltip."""
    body, feature, url = EXPLAIN[key]
    return body + (f"\n\n**Databricks:** {feature}" if feature else "") + (f" · [Learn more]({url})" if url else "")
