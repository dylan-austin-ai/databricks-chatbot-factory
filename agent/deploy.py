"""Log, register and deploy the shared agent (ARC-1, CAS-2..6, ADM-1).

qa:   python deploy.py --catalog qa_chatbot_factory --warehouse_id ... --endpoint chatbot-agent
prod: same with --require_gate true: refuses to deploy unless every live bot's
      golden set passed in QA against this platform version AND this exact git
      commit, and at least one QA chatbot has passed (CAS-4, REL-7). Every
      deployment is recorded in platform_releases with its source and configuration.

Endpoint is always-on (no scale-to-zero) so non-technical users never wait on a
cold start.

The script runs as numbered steps. It first checks what it can't build (catalog, warehouse,
model endpoints, secret scopes) and stops with the full list if anything is missing. It then
builds what it depends on, in order, waiting for each to be ready: control plane, AI Search
endpoint and shared index, runtime settings, model services, traces experiment, prompt.
Only then is the agent logged and deployed. It waits for the endpoint to serve the new version
before recording the release and removing the versions it replaced. Every step is safe to re-run.

Observability (OBS-6..12): every request is an MLflow trace stored in Unity Catalog Delta
tables (<catalog>._platform.traces_otel_*) behind one MLflow experiment, so traces are
queryable with SQL and viewable in the MLflow Traces UI. The system prompt is versioned in
the MLflow Prompt Registry. Answer models are Unity Gateway model services with their own
inference tables; usage lands in system.ai_gateway.usage tagged per bot (GW-1..6).
"""
import argparse
import inspect
import os
import sys
from pathlib import Path

import mlflow
from mlflow.models.auth_policy import AuthPolicy, SystemAuthPolicy, UserAuthPolicy
from mlflow.models.resources import (DatabricksServingEndpoint, DatabricksSQLWarehouse,
                                     DatabricksVectorSearchIndex)



def _root() -> Path:
    """Repository root. A serverless python-file task runs this file without __file__, so fall
    back to where Databricks exposes the script's folder: argv, the working directory and the
    import path."""
    try:
        return Path(__file__).resolve().parents[1]
    except NameError:
        pass
    for folder in [Path(sys.argv[0]).parent, Path.cwd(), *map(Path, sys.path)]:
        root = folder.resolve().parent
        if (root / "agent" / "agent.py").is_file() and (root / "src" / "factory").is_dir():
            return root
    raise RuntimeError("Cannot find the chatbot-factory source root from argv, cwd or sys.path.")


ROOT = _root()
sys.path.insert(0, str(ROOT / "src"))
from factory import PLATFORM_VERSION  # noqa: E402
from factory.answering import SYSTEM_PROMPT  # noqa: E402

p = argparse.ArgumentParser()
p.add_argument("--catalog", default="chatbots")
p.add_argument("--warehouse_id", required=True)
p.add_argument("--endpoint", default="chatbot-agent")
p.add_argument("--require_gate", default="false")
p.add_argument("--qa_catalog", default="")
p.add_argument("--git_commit", default="")
p.add_argument("--git_branch", default="")
p.add_argument("--git_origin", default="")
p.add_argument("--environment", default="qa")
p.add_argument("--identity_scope", default="chatbot-factory-identity")
p.add_argument("--agent_sp_scope", default="chatbot-factory")
p.add_argument("--agent_principal", default="")
p.add_argument("--usage_policy_id", default="")
a, _ = p.parse_known_args()

# Shared job setup (Spark, settings including the admin overrides, control plane, SDK client).
sys.path.insert(0, str(ROOT / "jobs"))
from _bootstrap import context, vector_client  # noqa: E402
from factory.gateway import (ensure_model_service, grant_execute, labels, logging_problems,  # noqa: E402
                             ui_checklist)
from factory.preflight import deploy_problems, gate_problems  # noqa: E402
from factory.provisioning import ensure_shared_index, grant_agent_access  # noqa: E402
from factory.serving import remove_old_versions, wait_until_serving  # noqa: E402

STEPS = 12


def step(n: int, what: str) -> None:
    print(f"[{n}/{STEPS}] {what}", flush=True)


spark, sql, s, cp, w = context(a.catalog)
mlflow.set_tracking_uri("databricks")
mlflow.set_registry_uri("databricks-uc")
model_name = f"{a.catalog}.{s.platform_schema}.chatbot_agent"

# Every step below needs only what the steps above it built or checked, so this job can run on
# a workspace where nothing but docs/SETUP.md Part A has been done.

step(1, "Checking what this job can't build itself")
problems = deploy_problems(w, s, a.warehouse_id, a.identity_scope, a.agent_sp_scope)
if problems:
    raise SystemExit("Missing prerequisites (docs/SETUP.md, Part A):\n  - " + "\n  - ".join(problems))

step(2, "Control plane: platform schema, volumes and tables")
cp.ensure()  # idempotent; shared_chunks and platform_releases are used below
grant_agent_access(sql, s, a.agent_principal)

step(3, "AI Search endpoint and shared index (the first run waits for provisioning)")
index_name = ensure_shared_index(vector_client(), s, usage_policy_id=a.usage_policy_id)

step(4, "Runtime settings file the agent reads at startup")
cp.publish(None)

# Answer models are Unity Gateway model services (GW-1): created here with their routing and
# fallbacks if missing, and otherwise left alone. EXECUTE is granted here; rate limits, the
# inference table and policies can only be set in the UI (docs/SETUP.md, Part C).
step(5, "Answer model services and who may call them")
callers = [x for x in (a.agent_principal, s.get("access.admin_group"), s.get("access.jobs_run_as")) if x]
execute_granted = set()
for label in labels(s):
    service = ensure_model_service(w, s, label)
    print("Model service ready:", service)
    # The agent calls the answer services; only MLOps needs the guardrail evaluator.
    who = [s.get("access.admin_group")] if label == "evaluator" else callers
    try:
        execute_granted.add(grant_execute(w, s, label, [x for x in who if x]))
        print(f"  EXECUTE granted to: {', '.join(x for x in who if x)}")
    except Exception as e:  # noqa: BLE001 - the agent is denied until this is granted by hand
        print(f"  WARNING: EXECUTE not granted on {service}: {str(e)[:200]}. Grant it in the UI (docs/SETUP.md, C2).")

# Traces in Unity Catalog, viewed through one MLflow experiment (OBS-9). The binding is made
# once, when the experiment is created, and can't be changed later.
step(6, "Traces experiment")
os.environ["MLFLOW_TRACING_SQL_WAREHOUSE_ID"] = a.warehouse_id
EXPERIMENT = f"/Shared/chatbot-factory/{a.environment}/traces"
experiment = mlflow.get_experiment_by_name(EXPERIMENT)
if experiment is None:
    from mlflow.entities.trace_location import UnityCatalog
    # MLflow doesn't create the workspace folder the experiment lives in.
    w.workspace.mkdirs(EXPERIMENT.rsplit("/", 1)[0])
    mlflow.create_experiment(EXPERIMENT, trace_location=UnityCatalog(
        catalog_name=a.catalog, schema_name=s.platform_schema, table_prefix="traces"))
    experiment = mlflow.get_experiment_by_name(EXPERIMENT)
mlflow.set_experiment(experiment_id=experiment.experiment_id)

# Answer prompt in the Prompt Registry (PRM-1). The agent serves the @production alias. A deploy
# moves @production only when the code's prompt changed, so an optimized prompt an admin promoted
# (jobs/optimize_prompt.py) survives redeploys of unchanged code.
step(7, "Answer prompt")
prompt_name = f"{a.catalog}.{s.platform_schema}.answer_prompt"
try:
    current = mlflow.genai.load_prompt(f"prompts:/{prompt_name}@code")
    code_changed = current.template != SYSTEM_PROMPT
except Exception:  # noqa: BLE001 - first deploy
    code_changed = True
if code_changed:
    prompt = mlflow.genai.register_prompt(
        name=prompt_name, template=SYSTEM_PROMPT,
        commit_message=f"platform {PLATFORM_VERSION} @ {a.git_commit or 'local'}",
        tags={"platform_version": PLATFORM_VERSION, "git_commit": a.git_commit, "source": "code"})
    for alias in ("code", "production"):
        mlflow.genai.set_prompt_alias(prompt_name, alias, version=prompt.version)

if a.require_gate == "true":
    step(8, "Release gate: QA quality checks for this version and commit")
    qa = a.qa_catalog or a.catalog
    try:
        bots = [r.asDict() for r in spark.sql(f"""
            WITH latest AS (
              SELECT bot_id, passed FROM `{qa}`.`{s.platform_schema}`.eval_runs
              WHERE platform_version = :pv AND git_commit = :gc
              QUALIFY ROW_NUMBER() OVER (PARTITION BY bot_id ORDER BY ts DESC) = 1)
            SELECT b.bot_id, coalesce(l.passed, false) AS passed,
                   b.state = 'live' AND (b.platform_pin IS NULL OR b.pin_expires_at < current_timestamp())
                     AS required
            FROM `{qa}`.`{s.platform_schema}`.bots b
            LEFT JOIN latest l USING (bot_id)
            WHERE b.deleted_at IS NULL""", args={"pv": PLATFORM_VERSION, "gc": a.git_commit}).collect()]
    except Exception as e:  # noqa: BLE001 - no evidence means no deploy
        raise SystemExit(f"Release gate failed (CAS-4): could not read QA results from catalog '{qa}'. "
                         f"It must be readable from this workspace. {str(e)[:300]}")
    blockers = gate_problems(bots, PLATFORM_VERSION, a.git_commit)
    if blockers:
        raise SystemExit("Release gate failed (CAS-4):\n  - " + "\n  - ".join(blockers))
else:
    step(8, "Release gate: not required for this target")

step(9, "Logging, registering and deploying the agent")
resources = [
    DatabricksSQLWarehouse(warehouse_id=a.warehouse_id),
    DatabricksVectorSearchIndex(index_name=index_name),
    DatabricksServingEndpoint(endpoint_name=s.get("models.judge_endpoint")),
]
auth = AuthPolicy(
    system_auth_policy=SystemAuthPolicy(resources=resources),
    # On-behalf-of-user: the agent checks UC grants as the caller (GOV-4)
    user_auth_policy=UserAuthPolicy(api_scopes=["sql.statement-execution", "sql.warehouses"]),
)

with mlflow.start_run(run_name=f"chatbot_agent_{PLATFORM_VERSION}"):
    info = mlflow.pyfunc.log_model(
        name="agent",
        python_model=str(ROOT / "agent" / "agent.py"),
        code_paths=[str(ROOT / "src" / "factory")],
        auth_policy=auth,
        pip_requirements=[
            "mlflow[databricks]>=3.14", "databricks-sdk>=0.60", "databricks-vectorsearch>=0.57",
            "databricks-ai-bridge>=0.6", "openai>=1.40", "pyyaml>=6", "cryptography>=42",
        ] + (["databricks-agentbricks"] if s.get("history.store") == "managed_sessions" else []),
        registered_model_name=model_name,
        metadata={"platform_version": PLATFORM_VERSION, "environment": a.environment,
                  "git_commit": a.git_commit},
    )

from databricks import agents  # noqa: E402

version = info.registered_model_version
sec = a.identity_scope
deploy_kwargs = dict(
    endpoint_name=a.endpoint, scale_to_zero=False,
    environment_vars={
        "FACTORY_CATALOG": a.catalog, "FACTORY_WAREHOUSE_ID": a.warehouse_id,
        "FACTORY_ENV": a.environment, "FACTORY_GIT_COMMIT": a.git_commit,
        # Traces -> UC-backed experiment (OBS-9); prompt version stamped on every trace (OBS-11)
        "MLFLOW_EXPERIMENT_ID": experiment.experiment_id, "ENABLE_MLFLOW_TRACING": "true",
        "MLFLOW_TRACING_SQL_WAREHOUSE_ID": a.warehouse_id, "FACTORY_PROMPT_NAME": prompt_name,
        "FACTORY_MASK_TRACES": str(s.get("observability.mask_pii", True)).lower(),
        # IDN-2: pseudonym and break-glass keys from a Security-only secret scope
        "FACTORY_IDENTITY_HASH_KEY": f"{{{{secrets/{sec}/{s.get('access.identity_hash_key')}}}}}",
        "FACTORY_IDENTITY_ENCRYPTION_KEY": f"{{{{secrets/{sec}/{s.get('access.identity_encryption_key')}}}}}",
        # Dedicated agent service principal: reads the runtime volume, writes the logs volume
        "DATABRICKS_CLIENT_ID": f"{{{{secrets/{a.agent_sp_scope}/agent-sp-client-id}}}}",
        "DATABRICKS_CLIENT_SECRET": f"{{{{secrets/{a.agent_sp_scope}/agent-sp-client-secret}}}}",
    },
    tags={s.get("tags.chatbot_name"): s.get("tags.shared_value"), "component": "chatbot-factory",
          "environment": a.environment, "platform_version": PLATFORM_VERSION,
          **({"git_commit": a.git_commit} if a.git_commit else {})},
)
if a.usage_policy_id:  # serverless usage policy tags on the endpoint's billing (CST-11)
    # usage_policy_id replaced budget_policy_id. agents.deploy ignores unknown keywords instead
    # of rejecting them, so pick the name this version actually has.
    known = inspect.signature(agents.deploy).parameters
    deploy_kwargs["usage_policy_id" if "usage_policy_id" in known else "budget_policy_id"] = a.usage_policy_id
agents.deploy(model_name, version, **deploy_kwargs)
from mlflow import MlflowClient  # noqa: E402
MlflowClient().set_registered_model_alias(model_name, "champion", version)

# Don't record or clean up anything for a deployment that didn't take.
step(10, "Waiting for the endpoint to serve the new version")
wait_until_serving(w, a.endpoint, model_name, version)

# Trace tables (OBS-9): explicit grants (ALL PRIVILEGES isn't enough for trace writes).
# Writers: the agent (spans) and the app (user feedback). Readers: MLOps and Security only,
# because traces hold raw questions and answers (PRV-1). Support uses redacted views.
step(11, "Trace table grants and the platform release record")
writers = [x for x in (a.agent_principal, s.get("access.app_service_principal")) if x]
readers = [s.get("access.admin_group"), s.get("access.security_group")]
for t in ("spans", "annotations", "logs", "metrics"):
    table = f"`{a.catalog}`.`{s.platform_schema}`.`traces_otel_{t}`"
    if not spark.catalog.tableExists(f"{a.catalog}.{s.platform_schema}.traces_otel_{t}"):
        print(f"WARNING: {table} doesn't exist yet, so its grants were skipped. "
              "Rerun this job once the experiment has created it.")
        continue
    for who in writers:
        spark.sql(f"GRANT MODIFY, SELECT ON TABLE {table} TO `{who}`")
    for who in readers:
        spark.sql(f"GRANT SELECT ON TABLE {table} TO `{who}`")

# Platform release record (REL-7, REL-8): who deployed what source with which configuration
cp.record_platform_release(
    PLATFORM_VERSION, a.git_commit, a.environment, str(version), a.require_gate == "true",
    w.current_user.me().user_name, git_branch=a.git_branch, git_origin=a.git_origin,
    config={"catalog": a.catalog, "workspace_id": str(w.get_workspace_id()), "endpoint": a.endpoint,
            "model": model_name, "warehouse_id": a.warehouse_id, "usage_policy_id": a.usage_policy_id,
            "agent_principal": a.agent_principal, "identity_scope": a.identity_scope,
            "agent_sp_scope": a.agent_sp_scope, "experiment_id": experiment.experiment_id,
            "prompt": prompt_name, "shared_index": index_name, "qa_catalog": a.qa_catalog})
print(f"Deployed {model_name} v{version} to {a.endpoint} (platform {PLATFORM_VERSION})")

# Every served version keeps compute running and the endpoint holds at most 15, so drop the
# versions this one replaced now that it is serving.
step(12, "Removing the agent versions this one replaced")
removed = remove_old_versions(w, agents, a.endpoint, model_name, version)
print("Removed versions:", ", ".join(removed) if removed else "none")

# What can only be set, or could not be set, from here.
todo = [i for i in ui_checklist(s) if not (i["setting"] == "Permission" and i["service"] in execute_granted)]
print("Set these in the Unity Gateway UI if not already set (docs/SETUP.md, Part C):")
for item in todo:
    print(f"  {item['service']}: {item['setting']} = {item['value']}")
for problem in logging_problems(s, spark.catalog.tableExists):
    print(f"WARNING: inference logging not verified. {problem}")
