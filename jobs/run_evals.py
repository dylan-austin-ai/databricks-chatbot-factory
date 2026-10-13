"""Golden-set evaluation on mlflow.genai.evaluate and the combined gate (EVG-1..5, EVL-6, CAS-4, JDG-1..4).

--bot_id X       one bot (candidate release by default; after doc/config changes, EVL-6)
--bot_id all     every non-draft bot (platform release gate in QA, CAS-4)
--channel live   evaluate the live release instead (post-promotion smoke test, REL-11)
--limit N        only the first N approved questions (smoke test)
--diagnose true  a small controlled run that prints, step by step, how long each thing the check
                 depends on takes (the chatbot, its own trace, reading a trace back, the judge
                 model), what every scorer said about every question and, every 90 seconds
                 something is still going, what every thread is waiting on

Each question runs through the deployed agent. Scores come from Databricks built-in judges
(Claude Haiku 4.5 by default) plus code scorers for the deterministic checks; every result is attached
to the question's trace and the run appears in MLflow's evaluation UI for side-by-side release
comparison. The pass/fail decision uses the results as the scorers return them, in this process,
so it does not wait on (or fail with) reading them back from trace storage. The golden set is mirrored into a Unity Catalog evaluation dataset per bot.
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
                                  RetrievalGroundedness, RetrievalRelevance, RetrievalSufficiency, Safety)

from _bootstrap import args, context

from factory import PLATFORM_VERSION, evals, llm
from factory.identity import is_platform_caller
from factory.ingestion import BotPaths
from factory.problems import safe_text
from factory.releases import index_table
from factory.tracing import as_dict, assessment_value, documents, question_of

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


def runs_as() -> str:
    """Who this job calls the chatbot as, worked out the way the agent works out its caller."""
    try:
        me = w.current_user.me()
        return me.user_name or (me.emails[0].value if me.emails else "") or me.display_name or ""
    except Exception as e:  # noqa: BLE001 - only used to explain a refusal
        print(f"could not read this job's own identity: {type(e).__name__}")
        return ""


RUNS_AS = runs_as()


def chatbot_fault(custom: dict) -> str:
    """Why there is nothing to score in what the chatbot returned (its error message, or it
    refused the check's own question), or "" when it gave an answer."""
    return evals.chatbot_fault(custom, RUNS_AS, is_platform_caller(RUNS_AS, settings))


# What happened to each question of this run, kept as it happens:
OUTCOMES: dict[str, str] = {}                # what the chatbot did with it, so a fault isn't judged as an answer
TRACE_IDS: dict[str, tuple[str, str]] = {}   # (this check's trace, the chatbot's own trace on the serving endpoint)
RESULTS: dict[str, dict[str, dict]] = {}     # what every scorer said about it: the gate decides from these
STATUS: dict[str, dict[str, str]] = {}       # how each scorer went: running, done, failed or skipped
# A judge that can't work without something in the question's expectations: it is skipped on a
# question that lacks it. Only "should refuse" questions carry guidelines of their own.
NEEDS = {"expectations_guidelines": "guidelines"}


def build_scorers(cfg, allowed: set[str], chunks: dict[str, str]) -> list:
    judges = {n: judge(cls, n, **kw) for n, (cls, kw) in BUILTINS.items() if cfg.judge_on(n)}
    if cfg.judge_on("custom") and cfg.custom_judge_instructions:
        from mlflow.genai.judges import make_judge
        judges["custom"] = make_judge(name="custom", model=judge_model, instructions=cfg.custom_judge_instructions
                                      + "\n\nQuestion: {{ inputs }}\nAnswer: {{ outputs }}")

    def checks(inputs, outputs, expectations, trace):
        custom = agent_outputs(trace)
        scores = evals.deterministic_scores(expectations.get("kind", "in_scope"),
                                            {"response": outputs, "custom": custom}, expectations, chunks, allowed)
        return [Feedback(name=k, value=v) for k, v in scores.items()]

    def kept(name, work):
        return evals.recording_scorer(name, work, RESULTS, STATUS, assessment_value, NEEDS.get(name, ""), safe_text)

    return [kept(n, j.run) for n, j in judges.items()] + [kept("checks", checks)]


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
        TRACE_IDS[question] = (mlflow.get_active_trace_id() or "", str(custom.get("trace_id") or ""))
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


def read_scores(run_id: str) -> tuple[int, dict[str, tuple[dict, dict]]]:
    """(how many traces a search of trace storage finds for the run, {question: (scores,
    rationales)} for the ones whose request can be read as a question). Only the diagnostic uses
    it now, to say whether that search works; the two are counted apart because a trace that is
    there but can't be decoded is a different fault from a trace that is missing."""
    out = {}
    traces = mlflow.search_traces(run_id=run_id, return_type="list", max_results=5000)
    for t in traces:
        question = question_of(t.data.request)   # storage returns the request as JSON text, sometimes twice over
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
    return len(traces), out


def stored_trace(trace_id: str):
    trace = mlflow.get_trace(trace_id) if trace_id else None
    if trace is None:
        raise LookupError("it has no trace id" if not trace_id else "it is not in trace storage")
    return trace


def stored_results(trace_id: str) -> str:
    """What trace storage holds against one of this check's traces, read back by its id."""
    found = [x for x in (getattr(stored_trace(trace_id).info, "assessments", None) or [])
             if getattr(x, "feedback", None) is not None]
    as_errors = sorted({x.name for x in found if getattr(x.feedback, "error", None) is not None})
    return (f"{len(found)} result(s): {', '.join(sorted({x.name for x in found})) or 'none'}"
            + (f". Stored as errors: {', '.join(as_errors)}" if as_errors else ""))


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
    for store in (OUTCOMES, TRACE_IDS, RESULTS, STATUS):
        store.clear()
    if cases and not DIAGNOSE:
        # One question first. If the chatbot itself is failing, or refuses the check's own
        # questions, there is nothing to score, and every judge would be marking its error or
        # its refusal as a bad answer.
        try:
            _, first = evals.call_agent(client, agent_ep, cfg.bot_id, cases[0]["question"], channel)
        except Exception as e:  # noqa: BLE001
            first = {"outcome": "error", "error": f"{type(e).__name__}: {str(e).splitlines()[0][:300] if str(e) else ''}"}
        if chatbot_fault(first):
            message = (f"The chatbot gave a test question no answer, so there is nothing to score for "
                       f"{cfg.bot_id}: {chatbot_fault(first)}")
            if len(bots) == 1:
                raise SystemExit(message)
            print(message)
            continue
    if DIAGNOSE and cases:   # each thing the check depends on, on its own, timed
        question = cases[0]["question"]
        print(f"DIAG: 0. this run calls the chatbot as {RUNS_AS or 'an identity that could not be read'}. "
              + ("The settings name that identity as one of the platform's own callers, so the chatbot should treat "
                 "these as test questions." if is_platform_caller(RUNS_AS, settings) else
                 "THE SETTINGS DO NOT NAME THAT IDENTITY as one of the platform's own callers (access.jobs_run_as, "
                 "access.synthetic_callers, access.trusted_callers), so the chatbot will treat these questions as "
                 "coming from an ordinary person and may refuse them."), flush=True)
        ok, answered = evals.timed("1. the chatbot answers one question",
                                   lambda: evals.call_agent(client, agent_ep, cfg.bot_id, question, channel), 120)
        if ok:
            said = answered[1] or {}
            print(f"DIAG: 1b. what it did with that question: {said.get('outcome')} "
                  f"(request {said.get('request_id') or 'not given'})"
                  + (f". THE CHATBOT GAVE NO ANSWER: {chatbot_fault(said)}" if chatbot_fault(said) else ""),
                  flush=True)
            # The traces in steps 2 to 6 are written by this job. This one was written by the serving
            # endpoint, under its own identity, a moment ago and in the background: so it is given
            # up to 90 seconds to arrive.
            agent_trace = str(said.get("trace_id") or "")

            def _endpoint_trace():
                if not agent_trace:
                    raise LookupError("the chatbot returned no trace id, so it was not tracing that request")
                found, why = evals.wait_for(lambda: mlflow.get_trace(agent_trace))
                if found is None:
                    raise LookupError("it is not in trace storage after 90 seconds" + (f" ({why})" if why else ""))
                return len(found.data.spans)

            ok, steps = evals.timed("1c. the chatbot's own trace, written by the serving endpoint, is read back "
                                    "by its id", _endpoint_trace, 150)
            print("DIAG: 1c. " + (
                f"it is stored, with {steps} step(s): the serving endpoint's traces are being saved" if ok else
                "THE SERVING ENDPOINT'S OWN TRACES ARE NOT PROVEN TO BE SAVED. The endpoint answered, so either its "
                "identity (the agent service principal) may not write the trace tables, or the endpoint is not "
                "sending its traces to this environment's trace storage. The deploy_agent job prints which trace "
                "grants it made and which were refused."), flush=True)

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
        evals.close_unfinished(RESULTS, STATUS)
        scored = {c["question"]: evals.scores_from(RESULTS.get(c["question"])) for c in cases}
        rows = [scored[c["question"]][0] for c in cases]
        faults = sum(1 for c in cases if OUTCOMES.get(c["question"]) in evals.CANT_BE_SCORED)
        scorer_errors = evals.scorer_problems([scored[c["question"]][2] for c in cases])
        if scorer_errors:
            print(f"{cfg.bot_id}: scorers that gave no score: {scorer_errors}", flush=True)
        if DIAGNOSE:
            print(f"DIAG: 9. what each question got, as the scorers returned it (not read back from storage): "
                  f"{sum(1 for r in rows if r)} of {len(rows)} question(s) have scores; the chatbot gave "
                  f"{faults} of them no answer", flush=True)
            for i, c in enumerate(cases, 1):
                q = c["question"]
                scores, _, errors = scored[q]
                ran = STATUS.get(q, {})
                print(f"DIAG:   question {i} ({q[:60]!r}): the chatbot's outcome was "
                      f"{OUTCOMES.get(q) or 'not recorded (it was never asked)'}. Scorers: "
                      + (", ".join(f"{n} {state}" for n, state in sorted(ran.items())) or "none ran"), flush=True)
                print(f"DIAG:   question {i} scores: "
                      + (", ".join(f"{n}={v:g}" for n, v in sorted(scores.items())) or "none"), flush=True)
                for name, why in sorted(errors.items()):
                    print(f"DIAG:   question {i} NO SCORE from {name}: {why}", flush=True)
                mine, endpoint = TRACE_IDS.get(q, ("", ""))
                ok, held = evals.timed(f"  question {i}: this check's trace is read back by its id",
                                       lambda t=mine: stored_results(t), 60)
                if ok:
                    print(f"DIAG:   question {i} storage holds {held}", flush=True)
                evals.timed(f"  question {i}: the chatbot's own trace for it is in storage",
                            lambda t=endpoint: stored_trace(t), 60)
            ok, searched = evals.timed("10. this run's traces are found by searching for the run (what MLflow's "
                                       "evaluation page does)", lambda: read_scores(run.info.run_id), 180)
            if ok:
                print("DIAG: 10. " + evals.search_report(searched[0], searched[1], [c["question"] for c in cases]),
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
                          agent_errors=faults, scorer_errors=scorer_errors)
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
                          "scorer_errors": scorer_errors,
                          "per_case": [
                              {"eval_id": c["eval_id"], "question": c["question"],
                               "outcome": OUTCOMES.get(c["question"], ""),
                               "scores": scored[c["question"]][0], "rationales": scored[c["question"]][1],
                               "errors": scored[c["question"]][2]} for c in cases]}),
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
