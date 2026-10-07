"""Answer-prompt optimization with MLflow (PRM-2). Run on demand by MLOps from the Admin page.

--action optimize  GEPA rewrites the shared answer prompt against approved golden questions
                   from live bots (judged by Correctness + platform rules). The result is a new
                   prompt version with alias @optimized; nothing changes for users yet.
--action promote   moves @production to @optimized (the app then re-checks every bot, EVL-6).
                   The agent picks the new prompt up within a minute.
"""
import time

import mlflow
from _bootstrap import args, context

from factory import guardrails as g
from factory import llm
from factory.answering import fill_system, format_sources, search_filters
from factory.config import DEFAULT_RETRIEVAL
from factory.ingestion import BotPaths
from factory.provisioning import index_name_for
from factory.tracing import pages

a = args("catalog", "action", "environment", "max_questions", "actor", "evals_job_id")
spark, sql, settings, cp, w = context(a.catalog)
s = settings
mlflow.set_tracking_uri("databricks")
mlflow.set_experiment(f"/Shared/chatbot-factory/{a.environment or 'dev'}/traces")
name = f"{s.catalog}.{s.platform_schema}.answer_prompt"


def record(status: str, **kw) -> None:
    sql.execute(f"""INSERT INTO {s.fq('prompt_optimizations')} VALUES (current_timestamp(), :n, :fv, :tv,
                    CAST(:i AS DOUBLE), CAST(:f AS DOUBLE), :st, :by)""",
                {"n": name, "fv": kw.get("from_version"), "tv": kw.get("to_version"), "i": kw.get("initial"),
                 "f": kw.get("final"), "st": status, "by": kw.get("by", "system:optimize")})


if a.action == "promote":
    opt = mlflow.genai.load_prompt(f"prompts:/{name}@optimized")
    mlflow.genai.set_prompt_alias(name, "production", version=opt.version)
    record("promoted", to_version=str(opt.version), by=a.actor or "admin")
    if a.evals_job_id:  # EVL-6: every bot is re-checked against the new prompt
        w.jobs.run_now(int(a.evals_job_id), job_parameters={"bot_id": "all", "trigger": "prompt_promoted"})
    print(f"@production -> v{opt.version}; re-check of every bot started")
    raise SystemExit(0)

from mlflow.genai.optimize import GepaPromptOptimizer  # noqa: E402
from mlflow.genai.scorers import Correctness, Guidelines  # noqa: E402
from databricks.vector_search.client import VectorSearchClient  # noqa: E402

judge = f"databricks:/{s.get('models.judge_endpoint')}"
gateway = llm.gateway_client(w, s.get("unity_gateway.base_path"))
vsc = VectorSearchClient(disable_notice=True)
rules = list(s.get("guardrails.platform_rules", {}).values())
cap = int(a.max_questions or s.get("prompt_optimization.max_questions", 100))

# Training data: approved in-scope golden questions with expected answers, across live bots.
train, configs = [], {}
for bot in [b for b in cp.list_bots() if b["state"] == "live"]:
    cfg = configs[bot["bot_id"]] = cp.get_config(bot["bot_id"])
    for c in sql.query(f"""SELECT question, expected_answer FROM {BotPaths(s, cfg.bot_id).t('golden_set')}
                           WHERE approved AND active AND kind = 'in_scope' AND length(expected_answer) > 0
                           ORDER BY rand() LIMIT {cap // 5 or 1}"""):
        train.append({"inputs": {"question": c["question"], "bot_id": cfg.bot_id},
                      "expectations": {"expected_response": c["expected_answer"]}})
train = train[:cap]
if len(train) < 10:
    raise SystemExit("Need at least 10 approved questions with expected answers across live bots.")


def predict(question: str, bot_id: str) -> str:
    """Same retrieval and prompt assembly as the agent, using the prompt version under test."""
    cfg = configs[bot_id]
    template = mlflow.genai.load_prompt(f"prompts:/{name}@production").template  # the optimizer swaps this
    strategy = {**DEFAULT_RETRIEVAL, **(cfg.retrieval or {})}
    res = vsc.get_index(s.get("ai_search.endpoint"), index_name_for(s, cfg)).similarity_search(
        query_text=question, num_results=int(strategy.get("context_results", 6)),
        columns=["chunk_id", "doc_name", "doc_version", "page_ids", "section", "chunk_to_retrieve"],
        filters=search_filters(bot_id, "live", int(time.time())), query_type=strategy.get("query_type", "hybrid"))
    cols = [c["name"] for c in res["manifest"]["columns"]]
    hits = [dict(zip(cols, r)) for r in res.get("result", {}).get("data_array", [])]
    texts = [g.strip_injection(h["chunk_to_retrieve"] or "")[0] for h in hits]
    system = fill_system(template, cfg, rules)
    resp = gateway.chat.completions.create(
        model=s.answer_service(cfg.answer_model), temperature=0, max_tokens=int(s.get("agent.max_output_tokens", 1200)),
        messages=[{"role": "system", "content": f"{system}\n\nSOURCES:\n{format_sources(hits, texts, pages)}"},
                  {"role": "user", "content": question}])
    out = g.parse_structured(resp.choices[0].message.content or "") or {}
    return out.get("answer") or out.get("status", "")


before = mlflow.genai.load_prompt(f"prompts:/{name}@production")
result = mlflow.genai.optimize_prompts(
    predict_fn=predict, train_data=train, prompt_uris=[f"prompts:/{name}@production"],
    optimizer=GepaPromptOptimizer(reflection_model=f"databricks:/{s.get('models.generation_endpoint')}",
                                  max_metric_calls=int(s.get("prompt_optimization.max_metric_calls", 150)),
                                  display_progress_bar=False),
    scorers=[Correctness(model=judge), Guidelines(name="platform_rules", guidelines=rules, model=judge)])
new = result.optimized_prompts[0]
mlflow.genai.set_prompt_alias(name, "optimized", version=new.version)
record("optimized", from_version=str(before.version), to_version=str(new.version),
       initial=result.initial_eval_score, final=result.final_eval_score)
print(f"v{before.version} -> v{new.version}: score {result.initial_eval_score} -> {result.final_eval_score}")
