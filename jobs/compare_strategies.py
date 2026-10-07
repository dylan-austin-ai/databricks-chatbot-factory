"""Compare retrieval strategies for a bot and apply the winner (RET-3).

Runs after ingestion, before evals, against the candidate channel. Uses all active
in-scope golden questions that know their source document (approved or not), so the
owner gets a good default before they've reviewed test questions.
"""
import json
import time
import uuid

from _bootstrap import args, context, vector_client

from factory import strategies
from factory.answering import search_filters
from factory.ingestion import BotPaths
from factory.provisioning import index_name_for

a = args("catalog", "bot_id")
spark, sql, settings, cp, w = context(a.catalog)
cfg = cp.get_config(a.bot_id)
p = BotPaths(settings, cfg.bot_id)
cases = sql.query(f"""SELECT question, source_doc_id, expected_pages FROM {p.t('golden_set')}
                      WHERE active AND kind = 'in_scope' AND source_doc_id IS NOT NULL""")
if len(cases) < 3:
    print("Not enough test questions to compare strategies yet; keeping the default.")
    raise SystemExit(0)

idx = vector_client().get_index(settings.get("ai_search.endpoint"), index_name_for(settings, cfg))


def search(question: str, strategy: dict) -> list[dict]:
    kwargs = dict(query_text=question, columns=["chunk_id", "doc_id", "page_ids"],
                  num_results=int(strategy.get("num_results", 8)),
                  filters=search_filters(cfg.bot_id, "candidate", int(time.time())),
                  query_type=strategy.get("query_type", "hybrid"))
    if strategy.get("rerank"):
        from databricks.vector_search.reranker import DatabricksReranker
        kwargs["reranker"] = DatabricksReranker(columns_to_rerank=["chunk_to_retrieve", "doc_name"])
    res = idx.similarity_search(**kwargs)
    names = [c["name"] for c in res["manifest"]["columns"]]
    rows = [dict(zip(names, r)) for r in res.get("result", {}).get("data_array", [])]
    return [{"rank": i, "doc_id": r["doc_id"],
             "pages": ", ".join(str(int(x) + 1) for x in (r.get("page_ids") or []))}
            for i, r in enumerate(rows, 1)]


results = []
for strat in settings.get("ai_search.strategies"):
    try:
        results.append((strat, strategies.evaluate(strat, cases, search)))
    except Exception as e:  # noqa: BLE001 - e.g. reranker unavailable; skip that strategy
        print(f"Skipped {strat['name']}: {e}")
chosen, metrics = strategies.choose(results)
explanation = strategies.explain(chosen, metrics, results)
run_id = str(uuid.uuid4())
for strat, m in results:
    sql.execute(f"INSERT INTO {settings.fq('strategy_results')} VALUES (:r, :b, :s, :m, "
                "CAST(:c AS BOOLEAN), :e, current_timestamp())",
                {"r": run_id, "b": cfg.bot_id, "s": strat["name"], "m": json.dumps(m),
                 "c": strat is chosen, "e": explanation if strat is chosen else ""})
new = {k: chosen[k] for k in ("query_type", "rerank", "num_results")}
new["context_results"] = min(6, int(chosen.get("num_results", 8)))
if new != {k: cfg.retrieval.get(k) for k in new}:
    cfg.retrieval = new
    cp.save_config(cfg, "system:strategy_compare", f"retrieval strategy: {chosen['name']}")
    from factory.releases import Releases
    Releases(sql, settings, cp).stage(cfg, "system:strategy_compare")  # refresh snapshot hashes
print(explanation)
