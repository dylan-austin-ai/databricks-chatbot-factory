"""Golden-set evaluation on mlflow.genai.evaluate and the combined gate (EVG-1..5, EVL-6, CAS-4, JDG-1..4).

--bot_id X       one bot (candidate release by default; after doc/config changes, EVL-6)
--bot_id all     every non-draft bot (platform release gate in QA, CAS-4)
--channel live   evaluate the live release instead (post-promotion smoke test, REL-11)
--limit N        only the first N approved questions (smoke test)
--diagnose true  a small controlled run that prints, step by step, how long each thing the check
                 depends on takes (the chatbot, reading a trace back, the judge model, one scorer)
                 and, every 90 seconds something is still going, what every thread is waiting on

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
from factory.tracing import as_dict, assessment_value, documents

a = args("catalog", "bot_id", "trigger", "channel", "limit", "environment", "diagnose")
DIAGNOSE = a.diagnose in ("true", "paced")   # "true": one at a time. "paced": at the check's normal pace
if DIAGNOSE:
    a.limit = a.limit or "3"
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
for _var, _value in evals.check_limits(settings).items():   # a few judge calls at a time, not a hundred
    os.environ.setdefault(_var, _value)                      # an explicit environment variable wins
if DIAGNOSE:
    import faulthandler
    import sys

    if a.diagnose == "true":
        os.environ["MLFLOW_GENAI_EVAL_MAX_WORKERS"] = os.environ["MLFLOW_GENAI_EVAL_MAX_SCORER_WORKERS"] = "1"
    print(f"DIAG: diagnostic run with mlflow {mlflow.__version__}: {a.limit} question(s), "
          f"{os.environ['MLFLOW_GENAI_EVAL_MAX_WORKERS']} question(s) and "
          f"{os.environ['MLFLOW_GENAI_EVAL_MAX_SCORER_WORKERS']} scorer(s) at a time, at most "
          f"{os.environ['MLFLOW_GENAI_EVAL_SCORER_RATE_LIMIT']} scorer call(s) a second. Every 90 seconds that it is "
          "still going, what each thread is doing is printed (look for 'Thread 0x' below).", flush=True)
    faulthandler.dump_traceback_later(90, repeat=True, file=sys.stderr)
_judge_problem = evals.judge_problem(client, settings.get("models.judge_endpoint"))
if _judge_problem:
    raise SystemExit(f"The judge model '{settings.get('models.judge_endpoint')}' did not answer a one-line test, so "
                     f"nothing was checked: {_judge_problem}. Check the endpoint name under Admin, Platform, Judge "
                     "model endpoint, and that the jobs' identity may query it.")

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
    """What the chatbot returned, from the trace. A span's outputs come back as the JSON text they
    were stored as, so they are turned back into a dictionary here."""
    spans = trace.search_spans(name="agent.response") if trace is not None else []
    return as_dict(spans[0].outputs) if spans else {}


def chatbot_error(custom: dict) -> str:
    """Why the chatbot answered with its error message, or "" when it gave a real answer. The
    reason is only returned to the platform's own test callers; without it, say where to look."""
    if (custom or {}).get("outcome") != "error":
        return ""
    return str(custom.get("error") or "the chatbot gave no reason (it shares one only with the platform's own "
               "test callers: check access.jobs_run_as under Admin, Platform, and that the agent has been "
               "redeployed)")


# What the chatbot did with each question of this run, so an error isn't judged as if it were an answer.
OUTCOMES: dict[str, str] = {}


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
    # One trace per question, holding everything below. Without this decorator each span here
    # became a trace of its own and the tags had no trace to land on ("No active trace found"),
    # so the judges and the checks were handed a trace with only part of what they need.
    @mlflow.trace(name="quality_check_question")
    def predict(question: str) -> str:
        try:
            response, custom = evals.call_agent(client, agent_ep, cfg.bot_id, question, channel)
        except Exception as e:  # noqa: BLE001
            response, custom = f"ERROR: {e}", {"outcome": "error"}
        OUTCOMES[question] = custom.get("outcome") or ""
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
        question = as_dict(t.data.request).get("question")
        if not question:
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
    n_approved = len(cases)
    approved_in_scope = sum(1 for c in cases if c["kind"] == "in_scope")   # what the "enough questions" rule counts
    if a.limit:
        cases = [c for c in cases if c["kind"] == "in_scope"][: int(a.limit)]
    # This also runs by itself at the end of every setup and every update of the test version.
    # Until enough questions are approved such a check can only fail on the count, after asking
    # and judging every question, so it is left for the update that follows the approvals. A
    # check someone asked for always runs.
    if a.trigger in ("docs_changed", "provisioned") and len(cases) < min_cases:
        print(f"{cfg.bot_id}: {len(cases)} approved test question(s), {min_cases} needed; automatic check skipped")
        continue
    if not a.limit:   # a fixed sample spread over the documents when there are many more than needed
        cap = int(settings.get("quality.check.max_questions", 50) or 0)
        cases = evals.sample_cases(cases, max(cap, min_cases) if cap > 0 else 0)
        print(f"{cfg.bot_id}: checking {len(cases)} of {n_approved} approved test question(s)")
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
    OUTCOMES.clear()
    if cases and not DIAGNOSE:
        # One question first. If the chatbot itself is failing there is nothing to score, and every
        # judge would be marking its error message as a bad answer.
        try:
            _, first = evals.call_agent(client, agent_ep, cfg.bot_id, cases[0]["question"], channel)
        except Exception as e:  # noqa: BLE001
            first = {"outcome": "error", "error": f"{type(e).__name__}: {str(e).splitlines()[0][:300] if str(e) else ''}"}
        if chatbot_error(first):
            message = (f"The chatbot answered a test question with an error instead of an answer, so there is "
                       f"nothing to score for {cfg.bot_id}: {chatbot_error(first)}")
            if len(bots) == 1:
                raise SystemExit(message)
            print(message)
            continue
    if DIAGNOSE and cases:   # each thing the check depends on, on its own, timed
        question = cases[0]["question"]
        ok, answered = evals.timed("1. the chatbot answers one question",
                                   lambda: evals.call_agent(client, agent_ep, cfg.bot_id, question, channel), 120)
        if ok:
            outcome = (answered[1] or {}).get("outcome")
            print(f"DIAG: 1b. what it did with that question: {outcome}"
                  + (f". THE CHATBOT IS FAILING: {chatbot_error(answered[1])}" if outcome == "error" else ""),
                  flush=True)

        @mlflow.trace(name="diagnostic_trace")
        def _one_trace() -> str:
            with mlflow.start_span(name="child") as span:
                span.set_outputs({"ok": True})
            return mlflow.get_current_active_span().trace_id

        def _steps(trace) -> int:
            if trace is None:
                raise LookupError("the trace was not found")
            return len(trace.search_spans(name="child"))

        def _judge() -> None:
            problem = evals.judge_problem(client, settings.get("models.judge_endpoint"))
            if problem:
                raise RuntimeError(problem)

        ok, trace_id = evals.timed("2. a trace is written", _one_trace, 60)
        evals.timed("3. pending traces are sent to storage", mlflow.flush_trace_async_logging, 120)
        if ok:
            ok, found = evals.timed("4. that trace is read back by its id", lambda: mlflow.get_trace(trace_id), 120)
            if ok:
                evals.timed("5. its steps are read", lambda: _steps(found), 60)
        evals.timed("6. recent traces are searched (through the SQL warehouse)",
                    lambda: len(mlflow.search_traces(max_results=1, return_type="list")), 180)
        evals.timed("7. the judge model answers one question", _judge, 90)
        print(f"DIAG: 8. the check itself, on {len(cases)} question(s), starts now", flush=True)
    started_at = time.time()
    with mlflow.start_run(run_name=f"{cfg.bot_id}-{channel}-{a.trigger or 'manual'}") as run:
        mlflow.set_tags({"bot_id": cfg.bot_id, "release_id": release_id, "channel": channel,
                         "platform_version": PLATFORM_VERSION, "git_commit": git_commit,
                         "trigger": a.trigger or "manual"})
        mlflow.genai.evaluate(data=records, predict_fn=make_predict(cfg, channel, p, chunks),
                              scorers=build_scorers(cfg, allowed, chunks), model_id=model_id)
        if DIAGNOSE:
            print(f"DIAG: 8. asking and scoring finished in {time.time() - started_at:.1f}s", flush=True)
        scored = read_scores(run.info.run_id)
        rows = [scored.get(c["question"], ({}, {}))[0] for c in cases]
        if DIAGNOSE:
            print(f"DIAG: 9. scores read back in {time.time() - started_at:.1f}s from the start; "
                  f"{sum(1 for r in rows if r)} of {len(rows)} question(s) have scores; the chatbot answered "
                  f"{sum(1 for c in cases if OUTCOMES.get(c['question']) == 'error')} of them with an error",
                  flush=True)
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
        # "Enough questions" is about how many are approved, not how many of them this check sampled.
        gate = evals.gate(metrics, thresholds, zero, zero_checks, n_in_scope if a.limit else approved_in_scope,
                          1 if a.limit else min_cases, override,
                          required=[r for r in required if r in thresholds], unscored=sum(1 for r in rows if not r),
                          agent_errors=sum(1 for c in cases if OUTCOMES.get(c["question"]) == "error"))
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
                          "n_cases": len(cases), "n_approved": n_approved, "mlflow_run_id": run.info.run_id,
                          "per_case": [
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
    elif channel == "live" and not a.limit:
        cp.resolve_alerts(cfg.bot_id, "eval_regression")
    print(cfg.bot_id, channel, "PASSED" if gate.passed else "FAILED", metrics, zero, gate.failures)
    if DIAGNOSE:
        faulthandler.cancel_dump_traceback_later()
        print("DIAG: done. Nothing was recorded as a quality result.", flush=True)
    elif a.limit and not gate.passed:  # the promote job treats a failed smoke test as a failure
        raise SystemExit(f"Smoke test failed for {cfg.bot_id}: {gate.failures}")
