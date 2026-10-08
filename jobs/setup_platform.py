"""Platform setup (re-runnable): control plane, views, grants, shared AI Search, platform alerts.

Grants (PRV-1, REL-10, GOV-10):
  * raw logs: Security + MLOps see everything; support reads the same tables through Unity
    Catalog ABAC column masks (ai_mask for text, hash for identities), see sql/governance.sql
  * logs volume: Security + MLOps only
  * runtime volume: read for the agent service principal; logs volume: write for it
  * everything owned by the app service principal; no one else gets MODIFY
"""
from _bootstrap import ACTOR, args, context, vector_client

from factory.controlplane import render
from factory.provisioning import ensure_shared_index, grant_agent_access, principal
from factory.sql import ident

a = args("catalog", "agent_principal", "usage_policy_id")
spark, sql, settings, cp, w = context(a.catalog)
s = settings
sql.execute(f"CREATE CATALOG IF NOT EXISTS {ident(s.catalog)}")
cp.ensure()
cp.ensure_views()

security, admins, support = (s.get("access.security_group"), s.get("access.admin_group"),
                             s.get("access.support_group"))
app_sp = s.get("access.app_service_principal")
platform = ident(s.catalog, s.platform_schema)
if app_sp:
    sql.execute(f"ALTER SCHEMA {platform} OWNER TO {principal(app_sp)}")
for grp in filter(None, {security, admins, support, a.agent_principal}):
    sql.execute(f"GRANT USE CATALOG ON CATALOG {ident(s.catalog)} TO {principal(grp)}")
    sql.execute(f"GRANT USE SCHEMA ON SCHEMA {platform} TO {principal(grp)}")
for grp in filter(None, {security, admins}):
    for t in ("request_log", "guardrail_events", "conversations", "user_access", "releases",
              "release_chunks", "index_history", "snapshots", "audit_log", "eval_runs",
              "monitoring_rollup", "synthetic_health", "cost_daily", "alerts"):
        sql.execute(f"GRANT SELECT ON TABLE {platform}.{ident(t)} TO {principal(grp)}")
    sql.execute(f"GRANT READ VOLUME ON VOLUME {platform}.`logs` TO {principal(grp)}")
if support:  # masked by ABAC policies for anyone outside Security and MLOps
    for t in ("request_log", "guardrail_events", "feedback", "alerts", "judge_results"):
        sql.execute(f"GRANT SELECT ON TABLE {platform}.{ident(t)} TO {principal(support)}")
    for v in ("v_bot_activity", "v_monitoring", "v_guardrails", "v_alerts_open", "v_releases", "v_eval_latest",
              "v_observability_daily", "v_quality_daily"):
        sql.execute(f"GRANT SELECT ON VIEW {platform}.{ident(v)} TO {principal(support)}")
grant_agent_access(sql, s, a.agent_principal)

# Governance (GOV-10..12) and cost views over system tables (CST-9/10). Each statement is tried
# on its own: features an account hasn't enabled are reported, not fatal.
fmt = dict(catalog=ident(s.catalog), platform=ident(s.platform_schema), security=principal(security),
           admins=principal(admins), tag_chatbot=s.get("tags.chatbot_name"),
           extra="".join(f", {principal(p)}" for p in (app_sp, s.get("access.jobs_run_as")) if p),
           **cp.deployment_scope())
for file in ("governance.sql", "system_views.sql"):
    for stmt in render(file, **fmt):
        try:
            sql.execute(stmt)
        except Exception as e:  # noqa: BLE001
            print(f"[{file}] skipped: {str(e)[:200]}")

def api(method: str, path: str, body: dict, what: str) -> None:
    try:
        w.api_client.do(method, path, body=body)
        print(f"{what}: enabled")
    except Exception as e:  # noqa: BLE001 - already enabled, or not available on this account
        print(f"{what}: {str(e)[:200]}")

# Automatic PII classification of every table in the catalog (Data Classification, GA)
api("POST", f"/api/data-classification/v1/catalogs/{s.catalog}/config",
    {"name": f"catalogs/{s.catalog}/config"}, "Data classification")
# Freshness/completeness anomaly detection on the control plane (data quality monitoring)
api("POST", "/api/data-quality/v1/monitors",
    {"object_type": "schema", "object_id": w.schemas.get(f"{s.catalog}.{s.platform_schema}").schema_id,
     "anomaly_detection_config": {"excluded_table_full_names": []}}, "Anomaly detection")

ensure_shared_index(vector_client(), settings, usage_policy_id=a.usage_policy_id)
cp.publish(None)  # settings.json for the agent

try:
    from factory.alerting import ensure_bot_alerts
    ensure_bot_alerts(w, s, None, s.get("warehouse_id"))  # platform alerts to MLOps
except Exception as e:  # noqa: BLE001
    print(f"Platform email alerts need manual setup: {e}")

cp.audit(ACTOR, None, "platform_setup", s.catalog, {})
print(f"Platform ready in catalog {s.catalog}")
