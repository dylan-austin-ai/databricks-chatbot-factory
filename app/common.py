"""Shared app plumbing: identity, services, permissions, job triggers."""
from __future__ import annotations

import logging
import os
import re
import sys
from functools import lru_cache
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from factory.access import resolve_groups  # noqa: E402
from factory.config import BotConfig, PlatformSettings  # noqa: E402
from factory.controlplane import ControlPlane  # noqa: E402
from factory.documents import Documents  # noqa: E402
from factory.sql import WarehouseRunner  # noqa: E402

CATALOG = os.environ.get("FACTORY_CATALOG", "chatbots")
WAREHOUSE_ID = os.environ.get("FACTORY_WAREHOUSE_ID", "")
JOB_IDS = {
    "provision": os.environ.get("PROVISION_JOB_ID"),
    "ingest": os.environ.get("INGEST_JOB_ID"),
    "evals": os.environ.get("EVALS_JOB_ID"),
    "reindex": os.environ.get("REINDEX_JOB_ID"),
    "promote": os.environ.get("PROMOTE_JOB_ID"),
    "optimize": os.environ.get("OPTIMIZE_JOB_ID"),
    "maintenance": os.environ.get("MAINTENANCE_JOB_ID"),
}
APP_VERSION = os.environ.get("FACTORY_APP_VERSION", "")

BADGE = {"green": "Good", "yellow": "Check it", "red": "Problem", "n/a": "Text file", None: "Reading…"}
STATE_LABEL = {
    "draft": "Draft", "provisioning": "Setting up", "provision_failed": "Setup failed",
    "testing": "Testing", "pending_approval": "Waiting for approval", "live": "Live",
    "paused": "Paused", "budget_paused": "Paused (budget reached)", "archived": "Archived",
    "deleted": "Deleted", "purged": "Purged",
}
OUTCOME_LABEL = {  # UI-004: users can tell these apart
    "answered": None, "no_source": "Not covered by my documents",
    "refused": "Outside what I can help with", "blocked": "Blocked by a safety rule",
    "denied": "No access", "unverified": "Couldn't verify, showing sources",
    "unavailable": "Not available right now", "error": "Something went wrong",
}
METRIC_LABEL = {  # plain-English names for the go-live gate (EVG-1)
    "hit_at_1": "Right document first", "hit_at_3": "In the top 3", "hit_at_5": "In the top 5",
    "mrr": "Overall ranking", "correctness": "Correct", "groundedness": "Sticks to sources",
    "relevance": "Relevant", "citation_accuracy": "Citations accurate",
    "refusal_accuracy": "Refuses correctly", "safety": "Safe",
}
RETRIEVAL_METRICS = ["hit_at_1", "hit_at_3", "hit_at_5", "mrr"]


@lru_cache(maxsize=1)
def app_client():
    from databricks.sdk import WorkspaceClient
    return WorkspaceClient()  # the app's service principal


def user_client():
    """Client acting as the signed-in user (Databricks Apps user authorization)."""
    from databricks.sdk import WorkspaceClient
    token = st.context.headers.get("X-Forwarded-Access-Token")
    if not token:
        return app_client()
    return WorkspaceClient(host=app_client().config.host, token=token, auth_type="pat")


def current_user() -> str:
    return st.context.headers.get("X-Forwarded-Email") or os.environ.get("FACTORY_DEV_USER", "dev@local")


@st.cache_resource
def _sql():
    return WarehouseRunner(app_client(), WAREHOUSE_ID, tags={"source": "app"})


@st.cache_data(ttl=60)
def _overrides() -> dict:
    base = PlatformSettings.load({"catalog": CATALOG})
    try:
        o = ControlPlane(_sql(), base).load_overrides()
    except Exception:  # noqa: BLE001
        o = {}
    o["catalog"] = CATALOG
    return o


def settings() -> PlatformSettings:
    return PlatformSettings.load(_overrides())


def cp() -> ControlPlane:
    """Control plane; republishes the agent's runtime files on every change."""
    return ControlPlane(_sql(), settings(), app_client())


def sql() -> WarehouseRunner:
    return _sql()


def docs() -> Documents:
    return Documents(_sql(), settings(), cp(), app_client())


@st.cache_data(ttl=300)
def user_groups(email: str) -> set[str]:
    """Groups of the signed-in person, cached per person for 5 minutes, read with their own
    token. A missing token, a failed call or a token for a different account grants nothing;
    every outcome is logged by factory.access.

    The directory lookup exists only for local development: it is used when FACTORY_DEV_USER is
    set and the request did not come through the Databricks Apps proxy (no sign-in header), and
    it runs with the developer's own credentials. The deployed app never uses it."""
    headers = st.context.headers
    has_token = bool(headers.get("X-Forwarded-Access-Token")) and email == current_user()
    local_dev = bool(os.environ.get("FACTORY_DEV_USER")) and not headers.get("X-Forwarded-Email")
    return resolve_groups(
        email,
        (lambda: user_client().current_user.me()) if has_token else None,
        (lambda e: app_client().users.list(filter=f'userName eq "{e}"', attributes="userName,groups"))
        if local_dev else None)


EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@st.cache_data(ttl=3600, show_spinner=False)
def all_groups() -> list[str]:
    """Workspace groups (synced from the company directory), for the group pickers (WIZ-11)."""
    try:
        return sorted({g.display_name for g in app_client().groups.list(attributes="displayName") if g.display_name})
    except Exception:  # noqa: BLE001
        return []


@st.cache_data(ttl=3600, show_spinner=False)
def user_exists(email: str) -> bool:
    try:
        return bool(list(app_client().users.list(filter=f'userName eq "{email}"', attributes="userName")))
    except Exception:  # noqa: BLE001 - can't check: don't block the owner
        return True


def check_emails(emails: list[str], what: str) -> list[str]:
    """Problems with a list of work emails: format first, then that each is a Databricks user."""
    bad = [e for e in emails if not EMAIL.match(e)]
    if bad:
        return [f"{what}: use work emails, e.g. jane.doe@company.com (not {', '.join(bad)})."]
    missing = [e for e in emails if not user_exists(e.lower())]
    return [f"{what}: no Databricks user with the email {', '.join(missing)}."] if missing else []


def is_admin() -> bool:
    return settings().get("access.admin_group") in user_groups(current_user())


# Access decisions are logged at INFO (who was resolved, how, which groups), and the default log
# level would drop them. Send them to the app's log even when nothing else configured logging.
_access_log = logging.getLogger("factory.access")
_access_log.setLevel(logging.INFO)
if not logging.getLogger().handlers and not _access_log.handlers:
    _access_log.addHandler(logging.StreamHandler())


def is_tester(bot: dict) -> bool:
    import json as _json
    cfg = _json.loads(bot.get("config_json") or "{}")
    return current_user().lower() in {t.lower() for t in cfg.get("testers", [])}


def can_manage(bot: dict) -> bool:
    """Owner person, owner group, reviewer, or MLOps."""
    u = current_user()
    return (is_admin() or u in (bot.get("owner_user"), bot.get("reviewer"))
            or bot.get("owner_group") in user_groups(u))


def my_bots() -> list[dict]:
    return [b for b in cp().list_bots() if can_manage(b)]


def pick_bot(label: str = "Chatbot") -> tuple[dict, BotConfig] | tuple[None, None]:
    bots = my_bots()
    if not bots:
        st.info("You don't have any chatbots yet. Create one from **Create a chatbot**.")
        return None, None
    ids = [b["bot_id"] for b in bots]
    default = ids.index(st.session_state["bot_id"]) if st.session_state.get("bot_id") in ids else 0
    choice = st.sidebar.selectbox("Current chatbot", ids, index=default,
                          format_func=lambda i: next(b["display_name"] for b in bots if b["bot_id"] == i))
    st.session_state["bot_id"] = choice
    bot = next(b for b in bots if b["bot_id"] == choice)
    return bot, cp().get_config(choice)


def run_job(kind: str, **params) -> int | None:
    job_id = JOB_IDS.get(kind)
    if not job_id:
        st.error(f"The {kind} job isn't configured. Contact MLOps.")
        return None
    params.setdefault("catalog", CATALOG)
    run = app_client().jobs.run_now(job_id=int(job_id),
                                    job_parameters={k: str(v) for k, v in params.items()})
    return run.run_id


@st.cache_resource
def start_probe() -> bool:
    """Cheap synthetic probe from the always-on app (OBS-1): no LLM calls."""
    from probe import start
    start(app_client(), settings())
    return True


def trace_experiment_id() -> str | None:
    try:
        name = f"/Shared/chatbot-factory/{os.environ.get('FACTORY_ENV', 'qa')}/traces"
        return app_client().experiments.get_by_name(name).experiment.experiment_id
    except Exception:  # noqa: BLE001 - not deployed yet
        return None


def _span(s) -> dict:
    return {"name": s.name, "type": str(getattr(s, "span_type", "") or ""), "span_id": s.span_id,
            "parent_id": s.parent_id, "start_ns": s.start_time_ns, "end_ns": s.end_time_ns or s.start_time_ns,
            "attributes": dict(s.attributes or {}), "inputs": s.inputs, "outputs": s.outputs}


@st.cache_data(ttl=600, show_spinner="Reading traces…")
def load_traces(bot_id: str | None, limit: int = 300, trace_id: str | None = None) -> list[dict]:
    """Recent MLflow traces (or one trace) as plain dicts: {trace_id, tags, spans}."""
    import mlflow
    mlflow.set_tracking_uri("databricks")
    if trace_id:
        found = [mlflow.get_trace(trace_id)]
    else:
        exp = trace_experiment_id()
        if not exp:
            return []
        found = mlflow.search_traces(experiment_ids=[exp], max_results=limit, return_type="list",
                                     filter_string=f"tags.bot_id = '{bot_id}'" if bot_id else None,
                                     order_by=["timestamp_ms DESC"])
    return [{"trace_id": t.info.trace_id, "tags": dict(t.info.tags or {}), "spans": [_span(s) for s in t.data.spans]}
            for t in found if t]
