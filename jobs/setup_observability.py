"""Observability setup, run after each agent deploy (OBS-6..12).

1. Production judges (MON-6): Databricks built-in LLM scorers run on live traces and attach
   their scores to each trace. Critical bots: 100%; others: the admin sample rate (20%).
2. Drift monitoring (OBS-12): a Databricks data quality monitor on request_log computes daily
   profile and drift metrics (PSI, KS, Wasserstein, JS, chi-squared) per bot and per release,
   and generates its own dashboard. question_drift (embedding drift) is monitored the same way.
"""
import mlflow
from _bootstrap import args, context

from factory.tracing import judge_filters, start_production_scorer

a = args("catalog", "environment", "warehouse_id")
spark, sql, settings, cp, w = context(a.catalog)
s = settings
mlflow.set_tracking_uri("databricks")
exp = mlflow.set_experiment(f"/Shared/chatbot-factory/{a.environment or 'dev'}/traces")

# 1. Production scorers ---------------------------------------------------------------------
from mlflow.genai.scorers import (ConversationCompleteness, Guidelines, KnowledgeRetention,  # noqa: E402
                                  RelevanceToQuery, RetrievalGroundedness, RetrievalRelevance,
                                  RetrievalSufficiency, Safety, ScorerSamplingConfig, UserFrustration,
                                  delete_scorer)

try:
    from mlflow.tracing import set_databricks_monitoring_sql_warehouse_id  # UC trace storage; MLflow >= 3.5
    set_databricks_monitoring_sql_warehouse_id(a.warehouse_id, experiment_id=exp.experiment_id)
except Exception as e:  # noqa: BLE001
    print(f"Monitoring warehouse not set: {e}")

judge_model = f"databricks:/{s.get('models.judge_endpoint')}"  # Claude Haiku 4.5 (JDG-1)
rules = list(s.get("guardrails.platform_rules", {}).values())
# registered name -> (switch tag, built-in scorer, arguments). 9 scorers x 2 sample rates = 18 of
# the 20 allowed per experiment. Multi-turn judges score whole sessions (conversation_id).
scorers = {
    "groundedness": ("groundedness", RetrievalGroundedness, {}),
    "retrieval_relevance": ("retrieval_relevance", RetrievalRelevance, {}),
    "retrieval_sufficiency": ("retrieval_sufficiency", RetrievalSufficiency, {}),
    "relevance": ("relevance", RelevanceToQuery, {}),
    "safety": ("safety", Safety, {}),
    "platform_rules": ("platform_rules", Guidelines, {"guidelines": rules}),
    "conversation_completeness": ("multi_turn", ConversationCompleteness, {}),
    "user_frustration": ("multi_turn", UserFrustration, {}),
    "knowledge_retention": ("multi_turn", KnowledgeRetention, {}),
}
rate = float(s.get("quality.judge_sample_rate", 0.2))
skipped = []
for name, (switch, cls, kw) in scorers.items():
    for critical, sample in ((True, 1.0), (False, rate)):
        reg = f"{name}_{'critical' if critical else 'sampled'}"
        try:
            delete_scorer(name=reg)
        except Exception:  # noqa: BLE001 - first run
            pass
        try:
            scorer = cls(name=name, model=judge_model, **kw)
        except TypeError:  # this MLflow version's built-in takes no model: Databricks-managed judge
            scorer = cls(name=name, **kw)
        if start_production_scorer(scorer, reg, ScorerSamplingConfig(
                sample_rate=sample, filter_string=judge_filters(critical, switch))):
            print(f"Scorer {reg}: {sample:.0%}")
        else:
            skipped.append(reg)
if skipped:
    print(f"WARNING: {len(skipped)} production scorers are not running on this MLflow version: "
          + ", ".join(skipped))

# 2. Drift monitors ------------------------------------------------------------------------
from databricks.sdk.service.catalog import (MonitorCronSchedule, MonitorInferenceLog,  # noqa: E402
                                            MonitorInferenceLogProblemType, MonitorTimeSeries)

user = w.current_user.me().user_name
for table, kind in ((s.fq("request_log"), "inference"), (s.fq("question_drift"), "series")):
    try:
        w.quality_monitors.get(table)
        exists = True
    except Exception:  # noqa: BLE001 - no monitor yet
        exists = False
    if exists:
        try:
            w.quality_monitors.run_refresh(table)
        except Exception as e:  # noqa: BLE001 - e.g. a refresh already running
            print(f"Refresh of {table} not started: {e}")
        continue
    common = dict(table_name=table, output_schema_name=f"{s.catalog}.{s.platform_schema}",
                  assets_dir=f"/Workspace/Users/{user}/chatbot-factory/monitors/{table.split('.')[-1]}",
                  schedule=MonitorCronSchedule(quartz_cron_expression="0 30 6 * * ?",
                                               timezone_id="America/Chicago"),
                  slicing_exprs=["bot_id", "channel"] if kind == "inference" else ["bot_id"])
    if kind == "inference":
        w.quality_monitors.create(**common, inference_log=MonitorInferenceLog(
            timestamp_col="ts", granularities=["1 day", "1 week"], model_id_col="release_id",
            prediction_col="outcome", problem_type=MonitorInferenceLogProblemType.PROBLEM_TYPE_CLASSIFICATION))
    else:
        w.quality_monitors.create(**common, time_series=MonitorTimeSeries(
            timestamp_col="day", granularities=["1 day", "1 week"]))
    print(f"Monitor created on {table}")

# 3. Metric views + Genie agent over the observability tables (OBS-15) ------------------------
import json  # noqa: E402

from factory.controlplane import render  # noqa: E402
from factory.sql import ident  # noqa: E402

for stmt in render("metric_views.sql", catalog=ident(s.catalog), platform=ident(s.platform_schema)):
    try:
        sql.execute(stmt)
    except Exception as e:  # noqa: BLE001
        print(f"Metric view skipped: {str(e)[:200]}")
for mv in ("mv_chatbot_usage", "mv_chatbot_quality"):
    for grp in filter(None, (s.get("access.admin_group"), s.get("access.security_group"), s.get("access.support_group"))):
        try:
            sql.execute(f"GRANT SELECT ON VIEW {s.fq(mv)} TO `{grp}`")
        except Exception as e:  # noqa: BLE001
            print(f"Grant on {mv} skipped: {e}")
obs = dict(s.get("observability", {}))
if not obs.get("genie_space_id"):
    try:
        tables = sorted(s.fq(t) for t in ("mv_chatbot_usage", "mv_chatbot_quality", "v_observability_daily",
                                         "question_drift", "alerts", "judge_results", "request_log"))
        space = w.genie.create_space(
            warehouse_id=a.warehouse_id, title="Chatbot Factory observability",
            description="Ask about chatbot usage, quality, cost, speed, drift and alerts. Raw text is masked "
                        "for anyone outside Security and MLOps.",
            serialized_space=json.dumps({"version": 2, "data_sources": {"tables": [{"identifier": t} for t in tables]}}))
        obs["genie_space_id"] = space.space_id
        cp.set_setting("observability", obs, "system:setup_observability")
        print(f"Genie agent created: {space.space_id}")
    except Exception as e:  # noqa: BLE001
        print(f"Genie agent not created: {e}")

# Unified Unity Gateway trace table (GW-6): readable by MLOps and Security once an admin enables it
if s.get("unity_gateway.trace_table"):
    for grp in (s.get("access.admin_group"), s.get("access.security_group")):
        try:
            sql.execute(f"GRANT SELECT ON TABLE {s.get('unity_gateway.trace_table')} TO `{grp}`")
        except Exception as e:  # noqa: BLE001
            print(f"Gateway trace table grant skipped: {e}")

# Unity Gateway inference tables (GW-7). v_gateway_calls: every call to the answer service, tied to
# our request_log through the request_id request tag. v_guardrail_verdicts: every LLM-based policy
# verdict from the evaluator (Log and Enforce mode), tied to the call through the gateway request_id.
views = {}
if s.get("unity_gateway.inference_table"):
    views["v_gateway_calls"] = f"""
        SELECT request_id AS gateway_request_id, request_tags['request_id'] AS request_id,
               request_tags['bot_id'] AS bot_id, request_tags['channel'] AS channel, event_time, invocation_id,
               status_code, latency_ms, destination_name,
               get_json_object(response, '$.databricks_service_policy.reason') AS blocked_reason, request, response
        FROM {s.get('unity_gateway.inference_table')}"""
if s.get("unity_gateway.evaluator_inference_table") and views:
    verdict = "get_json_object(e.response, '$.choices[0].message.content')"
    views["v_guardrail_verdicts"] = f"""
        SELECT e.event_time, e.request_id AS gateway_request_id, c.request_id, c.bot_id, c.channel,
               CAST(get_json_object({verdict}, '$.flagged') AS BOOLEAN) AS flagged,
               CAST(get_json_object({verdict}, '$.confidence') AS DOUBLE) AS confidence,
               get_json_object({verdict}, '$.reason') AS reason, e.request AS judge_prompt
        FROM {s.get('unity_gateway.evaluator_inference_table')} e
        LEFT JOIN (SELECT DISTINCT gateway_request_id, request_id, bot_id, channel
                   FROM {s.fq('v_gateway_calls')}) c ON c.gateway_request_id = e.request_id"""
for name, body in views.items():
    try:
        sql.execute(f"CREATE OR REPLACE VIEW {s.fq(name)} AS {body}")
        for who in (s.get("access.admin_group"), s.get("access.security_group")):
            sql.execute(f"GRANT SELECT ON VIEW {s.fq(name)} TO `{who}`")
    except Exception as e:  # noqa: BLE001
        print(f"{name} not created: {e}")

# The app reads drift output for the Monitoring page (aggregates and anonymous 2-D points only).
app_sp = s.get("access.app_service_principal")
for t in ("request_log_profile_metrics", "request_log_drift_metrics", "question_drift",
          "question_drift_profile_metrics", "question_drift_drift_metrics", "question_embeddings"):
    if not app_sp:
        break
    try:
        sql.execute(f"GRANT SELECT ON TABLE {s.fq(t)} TO `{app_sp}`")
    except Exception as e:  # noqa: BLE001 - monitor tables appear after the first refresh
        print(f"Grant on {t} skipped: {e}")
