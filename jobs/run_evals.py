"""Golden-set evaluation on mlflow.genai.evaluate and the combined gate (EVG-1..5, EVL-6, CAS-4, JDG-1..4).

--bot_id X       one bot (candidate release by default; after doc/config changes, EVL-6)
--bot_id all     every non-draft bot (platform release gate in QA, CAS-4)
--channel live   evaluate the live release instead (post-promotion smoke test, REL-11)
--limit N        only the first N approved questions (smoke test)

Each question runs through the deployed agent. Scores come from Databricks built-in judges
(Claude Haiku 4.5 by default) plus code scorers for the deterministic checks; every result is attached
to the question's trace and the run appears in MLflow's evaluation UI for side-by-side release
comparison. The golden set is mirrored into a Unity Catalog evaluation dataset per bot.
Per-bot options (admin): which judges run, a custom make_judge judge, and a simulated
multi-turn pass with conversation judges (informational, not part of the gate).
"""
import json
import os
import time
import uuid

import mlflow
from mlflow.entities import Feedback
from mlflow.genai.scorers import (Correctness, ExpectationsGuidelines, Guidelines, RelevanceToQuery,
                                  RetrievalGroundedness, RetrievalRelevance, RetrievalSufficiency, Safety,
                                  scorer)

from _bootstrap import args, context

from factory import PLATFORM_VERSION, evals, llm
from factory.ingestion import BotPaths
from factory.releases import index_table
from factory.tracing import assessment_value, documents

a = args("catalog", "bot_id", "trigger", "channel", "limit", "environment")
spark, sql, settings, cp, w = context(a.catalog)
client = llm.openai_client(w)
agent_ep = settings.get("agent.serving_endpoint")
judge_model = f"databricks:/{settings.get('models.judge_endpoint')}"
thresholds = settings.thresholds()
zero_checks = settings.get("quality.zero_failure_checks", list(evals.ZERO_CHECKS))
min_cases = int(settings.get("quality.min_golden_questions", 50))
rules = list(settings.get("guardrails.platform_rules", {}).values())
git_commit = os.environ.get("FACTORY_GIT_COMMIT", "")
mlflow.set_tracking_uri("databricks")
mlflow.set_experiment(f"/Shared/chatbot-factory/{a.environment or 'qa'}/traces")

BUILTINS = {"correctness": (Correctness, {}), "groundedness": (RetrievalGroundedness, {}),
            "retrieval_relevance": (RetrievalRelevance, {}), "retrieval_sufficiency": (RetrievalSufficiency, {}),
            "relevance": (RelevanceToQuery, {}), "safety": (Safety, {}),
            "expectations_guidelines": (ExpectationsGuidelines, {}),
            "platform_rules": (Guidelines, {"guidelines": rules})}


def judge(cls, name: str, **kw):
    try:
        return cls(name=name, model=judge_model, **kw)
    except TypeError:  # built-in without model=: Databricks-managed judge
        return cls(name=name, **kw)


def agent_outputs(trace) -> dict:
    spans = trace.search_spans(name="agent.response")
    return (spans[0].outputs or {}) if spans else {}


def build_scorers(cfg, allowed: set[str], chunks: dict[str, str]) -> list:
    out = [judge(cls, n, **kw) for n, (cls, kw) in BUILTINS.items() if cfg.judge_on(n)]
    if cfg.judge_on("custom") and cfg.custom_judge_instructions:
        from mlflow.genai.judges import make_judge
        out.append(make_judge(name="custom", model=judge_model, instructions=cfg.custom_judge_instructions
                              + "\n\nQuestion: {{ inputs }}\nAnswer: {{ outputs }}"))

    @scorer
    def checks(outputs, expectations, trace):
        custom = agent_outputs(trace)
        scores = evals.deterministic_scores(expectations.get("kind", "in_scope"),
                                            {"response": outputs, "custom": custom}, expectations, chunks, allowed)
        return [Feedback(name=k, value=v) for k, v in scores.items()]

    return out + [checks]


def make_predict(cfg, channel: str, p: BotPaths, chunks: dict[str, str]):
    def predict(question: str) -> str:
        try:
            response, custom = evals.call_agent(client, agent_ep, cfg.bot_id, question, channel)
        except Exception as e:  # noqa: BLE001
            response, custom = f"ERROR: {e}", {"outcome": "error"}
        hits = custom.get("retrieval") or []
        need = [h["chunk_id"] for h in hits if h.get("chunk_id") and h["chunk_id"] not in chunks]
        if need:
            chunks.update({r["chunk_id"]: r["chunk_to_retrieve"] for r in sql.query(
                f"SELECT chunk_id, chunk_to_retrieve FROM {p.t('chunked')} WHERE chunk_id IN "
                f"({', '.join(f':c{i}' for i in range(len(need)))})", {f"c{i}": c for i, c in enumerate(need)})})
        # The agent runs remotely: mirror its retrieval here so retrieval judges can read it.
        with mlflow.start_span(name="retrieve", span_type="RETRIEVER") as span:
            span.set_inputs({"query": question})
            span.set_outputs(documents([{**h, "chunk_to_retrieve": chunks.get(h.get("chunk_id"), "")} for h in hits]))
        with mlflow.start_span(name="agent.response", span_type="PARSER") as span:
            span.set_outputs(custom)
        mlflow.update_current_trace(tags={"bot_id": cfg.bot_id, "channel": channel,
                                          "agent_trace_id": str(custom.get("trace_id") or "")})
        return custom.get("answer") or response
    return predict


def read_scores(run_id: str) -> dict[str, tuple[dict, dict]]:
    """question -> (scores, rationales) from the assessments on the run's traces."""
    out = {}
    for t in mlflow.search_traces(run_id=run_id, return_type="list", max_results=5000):
        try:
            question = json.loads(t.data.request).get("question")
        except Exception:  # noqa: BLE001
            continue
        scores, why = {}, {}
        for x in getattr(t.info, "assessments", None) or []:
            fb = getattr(x, "feedback", None)
            if fb is None:
                continue  # expectations, not results
            v = assessment_value(fb.value)
            if v is not None:
                scores[x.name], why[x.name] = v, x.rationale or ""
        out[question] = (scores, why)
    return out


def simulate(cfg, channel: str, cases: list[dict]) -> None:
    """Optional, informational: simulated multi-turn conversations scored by conversation judges."""
    try:
        from mlflow.genai.scorers import ConversationCompleteness, KnowledgeRetention, UserFrustration
        from mlflow.genai.simulators import ConversationSimulator
        sim = ConversationSimulator(max_turns=3, user_model=judge_model, test_cases=[
            {"goal": f"Get a correct, sourced answer to: {c['question']}, then ask one follow-up.",
             "persona": "A busy employee who writes short questions."} for c in cases[:10]])

        def converse(input=None, messages=None, **_):  # noqa: A002 - simulator passes the history
            history = input or messages or []
            text, _c = evals.call_agent(client, agent_ep, cfg.bot_id, history[-1]["content"], channel)
            return text

        mlflow.genai.evaluate(data=sim, predict_fn=converse, scorers=[
            judge(ConversationCompleteness, "conversation_completeness"),
            judge(UserFrustration, "user_frustration"), judge(KnowledgeRetention, "knowledge_retention")])
    except Exception as e:  # noqa: BLE001
        print(f"{cfg.bot_id}: conversation simulation skipped: {e}")


bots = [b for b in cp.list_bots() if b["state"] not in ("draft", "provisioning", "archived", "deleted")] \
    if a.bot_id in ("", "all") else [cp.get_bot(a.bot_id)]

for bot in bots:
    cfg = cp.get_config(bot["bot_id"])
    channel = a.channel or ("candidate" if bot.get("candidate_release_id") else "live")
    release_id = bot.get("candidate_release_id") if channel == "candidate" else bot.get("live_release_id")
    if not release_id:
        print(f"{cfg.bot_id}: no {channel} release to evaluate")
        continue
    p = BotPaths(settings, cfg.bot_id)
    flag = "in_candidate" if channel == "candidate" else "in_live"
    now = int(time.time())
    allowed = {r["chunk_id"] for r in sql.query(
        f"""SELECT chunk_id FROM {index_table(settings, cfg)} WHERE bot_id = :b AND {flag}
            AND effective_ts <= :n AND expires_ts > :n""", {"b": cfg.bot_id, "n": now})}
    cases = sql.query(f"SELECT * FROM {p.t('golden_set')} WHERE approved AND active "
                      "AND kind IN ('in_scope', 'out_of_scope', 'feedback') ORDER BY difficulty, eval_id")
    if a.limit:
        cases = [c for c in cases if c["kind"] == "in_scope"][: int(a.limit)]
    records = [evals.dataset_record(c) for c in cases]

    try:  # golden set mirrored as a native UC evaluation dataset (browse/compare in MLflow)
        import mlflow.genai.datasets as datasets
        ds_name = f"{settings.catalog}.{cfg.bot_id}.eval_dataset"
        try:
            ds = datasets.get_dataset(name=ds_name)
        except Exception:  # noqa: BLE001
            ds = datasets.create_dataset(name=ds_name)
        ds.merge_records(records)
    except Exception as e:  # noqa: BLE001
        print(f"{cfg.bot_id}: evaluation dataset not synced: {e}")

    model_id = None
    if cfg.obs_on("link_versions"):  # LoggedModel per release: traces and evals link to the version
        try:
            name = f"{cfg.bot_id}-{release_id}"
            found = mlflow.search_logged_models(filter_string=f"name = '{name}'", max_results=1, output_format="list")
            model_id = (found[0] if found else mlflow.create_external_model(name=name)).model_id
            mlflow.log_model_params({"release_id": release_id, "config_version": str(bot["config_version"]),
                                     "platform_version": PLATFORM_VERSION, "git_commit": git_commit}, model_id=model_id)
            sql.execute(f"UPDATE {settings.fq('releases')} SET model_id = :m WHERE release_id = :r",
                        {"m": model_id, "r": release_id})
        except Exception as e:  # noqa: BLE001
            print(f"{cfg.bot_id}: version link skipped: {e}")

    chunks: dict[str, str] = {}
    with mlflow.start_run(run_name=f"{cfg.bot_id}-{channel}-{a.trigger or 'manual'}") as run:
        mlflow.set_tags({"bot_id": cfg.bot_id, "release_id": release_id, "channel": channel,
                         "platform_version": PLATFORM_VERSION, "git_commit": git_commit,
                         "trigger": a.trigger or "manual"})
        mlflow.genai.evaluate(data=records, predict_fn=make_predict(cfg, channel, p, chunks),
                              scorers=build_scorers(cfg, allowed, chunks), model_id=model_id)
        scored = read_scores(run.info.run_id)
        rows = [scored.get(c["question"], ({}, {}))[0] for c in cases]
        metrics, zero = evals.aggregate(rows), evals.zero_failures(rows)
        rel = sql.query(f"SELECT safety_override_by FROM {settings.fq('releases')} WHERE release_id = :r",
                        {"r": release_id})
        override = bool(rel and rel[0]["safety_override_by"])
        n_in_scope = sum(1 for c in cases if c["kind"] == "in_scope")
        in_scope = [c for c in cases if c["kind"] == "in_scope"]
        required = [j for j in ("groundedness", "relevance", "safety") if cfg.judge_on(j)] + \
            (["correctness"] if cfg.judge_on("correctness") and any(c.get("expected_answer") for c in in_scope) else []) + \
            (["citation_accuracy"] if in_scope else []) + \
            (["refusal_accuracy"] if any(c["kind"] == "out_of_scope" for c in cases) else []) + \
            (["hit_at_1", "hit_at_3", "hit_at_5", "mrr"] if any(c.get("source_doc_id") for c in in_scope) else [])
        gate = evals.gate(metrics, thresholds, zero, zero_checks, n_in_scope, 1 if a.limit else min_cases, override,
                          required=[r for r in required if r in thresholds], unscored=sum(1 for r in rows if not r))
        mlflow.set_tags({"passed": str(gate.passed)})
        mlflow.log_metrics({f"gate_{k}": v for k, v in metrics.items()})
    if not a.limit and (cfg.obs_on("simulate_conversations") or cfg.judge_on("multi_turn")):
        simulate(cfg, channel, [c for c in cases if c["kind"] == "in_scope"])

    run_id = str(uuid.uuid4())
    sql.execute(
        f"""INSERT INTO {settings.fq('eval_runs')} VALUES (:id, :b, :rel, :ch, :t, :pv, :gc,
            CAST(:cv AS INT), :m, CAST(:p AS BOOLEAN), CAST(:nr AS BOOLEAN), current_timestamp())""",
        {"id": run_id, "b": cfg.bot_id, "rel": release_id, "ch": channel, "t": a.trigger or "manual",
         "pv": PLATFORM_VERSION, "gc": git_commit, "cv": bot["config_version"],
         "m": json.dumps({"metrics": metrics, "zero_failures": zero, "failures": gate.failures,
                          "n_cases": len(cases), "mlflow_run_id": run.info.run_id, "per_case": [
                              {"eval_id": c["eval_id"], "question": c["question"],
                               "scores": scored.get(c["question"], ({}, {}))[0],
                               "rationales": scored.get(c["question"], ({}, {}))[1]} for c in cases]}),
         "p": gate.passed, "nr": gate.needs_safety_review})
    if channel == "candidate" and not a.limit:
        sql.execute(f"""UPDATE {settings.fq('releases')} SET eval_run_id = :e, eval_passed = CAST(:p AS BOOLEAN),
                        updated_at = current_timestamp() WHERE release_id = :r""",
                    {"e": run_id, "p": gate.passed, "r": release_id})
        cp.publish(cfg.bot_id)
    if not gate.passed and channel == "live" and not a.limit:
        cp.alert(cfg.bot_id, "eval_regression", "high", "; ".join(gate.failures),
                 dedupe_key=f"eval:{cfg.bot_id}:{release_id}:{PLATFORM_VERSION}")
    print(cfg.bot_id, channel, "PASSED" if gate.passed else "FAILED", metrics, zero, gate.failures)
    if a.limit and not gate.passed:  # the promote job treats a failed smoke test as a failure
        raise SystemExit(f"Smoke test failed for {cfg.bot_id}: {gate.failures}")
