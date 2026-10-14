"""Shared app plumbing: identity, services, permissions, job triggers."""
from __future__ import annotations

import json
import logging
import os
import re
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from factory import jobruns, journey, problems  # noqa: E402
from factory.access import resolve_groups  # noqa: E402
from factory.config import BotConfig, PlatformSettings  # noqa: E402
from factory.controlplane import ControlPlane  # noqa: E402
from factory.documents import Documents  # noqa: E402
from factory.ingestion import BotPaths  # noqa: E402
from factory.jobruns import covered_by  # noqa: E402
from factory.sql import WarehouseRunner, union_by_key  # noqa: E402
from factory.wizard import UNFINISHED_STATES  # noqa: E402

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
    "refusal_accuracy": "Refuses correctly", "rule_compliance": "Rule questions handled correctly",
    "platform_rules": "Platform rules followed (all questions)", "answer_rate": "Answerable questions answered",
    "safety": "Safe",
}
RETRIEVAL_METRICS = ["hit_at_1", "hit_at_3", "hit_at_5", "mrr"]
PAGES = {  # where the "what to do next" steps send people: page file and the name shown in the menu
    "create": ("pages/create.py", "Create chatbot"), "documents": ("pages/documents.py", "Documents"),
    "test_questions": ("pages/test_questions.py", "Test questions"), "launch": ("pages/launch.py", "Launch"),
}
READ_TTL = 30  # seconds a page read is reused before the warehouse is asked again


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


@st.cache_data(ttl=READ_TTL, show_spinner=False)
def _read(statement: str, params: str) -> list[dict]:
    return _sql().query(statement, json.loads(params) if params else None)


@st.cache_data(ttl=600, show_spinner=False, max_entries=200)
def _read_steady(statement: str, params: str) -> list[dict]:
    return _sql().query(statement, json.loads(params) if params else None)


def forget(settings_too: bool = False) -> None:
    """Drop every remembered read, so the next one comes fresh from Databricks."""
    _read.clear()
    _read_steady.clear()
    if settings_too:
        _overrides.clear()


class AppSql:
    """How the app talks to the SQL warehouse.

    Streamlit runs a page again from the top on every click, so without this the same SELECTs
    would be sent for every click. Page reads are remembered for READ_TTL seconds. Anything that
    writes forgets them all, so people always see their own change straight away; changes made
    by background jobs show up within READ_TTL seconds, or at once with the Refresh button.

    The services that decide something before they write (ControlPlane, Documents) are built
    with fresh=True: their reads always go to the warehouse, so no decision is made on a
    remembered answer."""

    parallel = True

    def __init__(self, fresh: bool = False):
        self.fresh = fresh

    def query(self, statement, params=None):
        if self.fresh:
            return _sql().query(statement, params)
        return _read(statement, json.dumps(params, sort_keys=True, default=str) if params else "")

    def query_steady(self, statement, params=None):
        """For large reads of data that only changes when a document is read again (what was
        read from each page): remembered for 10 minutes, or until a write or Refresh."""
        return _read_steady(statement, json.dumps(params, sort_keys=True, default=str) if params else "")

    def execute(self, statement, params=None, keep_reads: bool = False):
        """`keep_reads` is for small, frequent writes nothing on screen depends on (a thumbs-up):
        forgetting every page's reads for each one would undo the remembering."""
        try:
            _sql().execute(statement, params)
        finally:
            if not keep_reads:
                forget()


@st.cache_data(ttl=300)
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
    return ControlPlane(AppSql(fresh=True), settings(), app_client())


def sql() -> AppSql:
    """For the pages: reads are remembered for a short while (see AppSql)."""
    return AppSql()


def docs() -> Documents:
    return Documents(AppSql(fresh=True), settings(), cp(), app_client())


def fresh_sql() -> AppSql:
    """For a service a page builds itself (the Provisioner): reads go to the warehouse every time."""
    return AppSql(fresh=True)


def refresh() -> None:
    """The Refresh button: forget everything remembered, including who is in which group."""
    forget(settings_too=True)
    user_groups.clear()


def docs_view() -> Documents:
    """The document service for showing things: its reads are the remembered ones. Use docs() to
    change anything."""
    return Documents(sql(), settings(), ControlPlane(sql(), settings()))


@st.cache_data(ttl=1800, show_spinner=False, max_entries=48)
def page_picture(path: str, max_width: int = 1500) -> tuple[bytes, float]:
    """A page image from the volume, made small enough to send to the browser quickly, and how
    much it was scaled by. Page images never change once written, so they are kept for a while
    instead of being downloaded again on every click."""
    import io

    from PIL import Image
    img = Image.open(io.BytesIO(app_client().files.download(path).contents.read())).convert("RGB")
    scale = min(1.0, max_width / img.width)
    if scale < 1.0:
        img = img.resize((max_width, max(1, round(img.height * scale))))
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=82)
    return out.getvalue(), scale


def all_bots(include_deleted: bool = False) -> list[dict]:
    """Every chatbot's row. Needed on every page, so it goes through the remembered reads."""
    return ControlPlane(sql(), settings()).list_bots(include_deleted)


def open_alerts() -> list[dict]:
    """Alerts that are unresolved and still true, newest first (a remembered read)."""
    return ControlPlane(sql(), settings()).open_alerts()


def bot_config(bot: dict) -> BotConfig:
    """A chatbot's configuration from its row, without asking the warehouse for it again."""
    return BotConfig.from_dict(json.loads(bot["config_json"]))


def read_many(queries: dict[str, tuple[str, dict | None]]) -> tuple[dict[str, list[dict]], dict[str, Exception]]:
    """Several independent reads at the same time instead of one after another. Returns the rows
    by name and, separately, the reads that failed (their rows are an empty list), so one missing
    table costs a page one section rather than the whole page."""
    from streamlit.runtime.scriptrunner import add_script_run_ctx, get_script_run_ctx
    ctx, reader = get_script_run_ctx(), sql()

    def one(item):
        add_script_run_ctx(threading.current_thread(), ctx)
        name, (statement, params) = item
        try:
            return name, reader.query(statement, params), None
        except Exception as e:  # noqa: BLE001 - handed back to the page, which decides what to show
            return name, [], e

    if not queries:
        return {}, {}
    with ThreadPoolExecutor(max_workers=min(8, len(queries))) as pool:
        results = list(pool.map(one, queries.items()))
    return {name: rows for name, rows, _ in results}, {name: e for name, _, e in results if e is not None}


def across_bots(bots: list[dict], select) -> tuple[list[dict], list[str]]:
    """The same SELECT against each chatbot's own tables, as one statement. `select(paths)`
    returns the SELECT for one chatbot; every row comes back with its bot_id. If the statement
    fails because one chatbot's tables are missing, each chatbot is read on its own (side by
    side) and the ones that can't be read are returned as the second value."""
    selects = {b["bot_id"]: select(BotPaths(settings(), b["bot_id"])) for b in bots}
    if not selects:
        return [], []
    try:
        statement, params = union_by_key(selects)
        return sql().query(statement, params), []
    except Exception:  # noqa: BLE001
        found, failed = read_many({b: union_by_key({b: one}) for b, one in selects.items()})
        return [r for rows in found.values() for r in rows], sorted(failed)


def bot_progress(bots: list[dict]) -> dict[str, dict]:
    """Document and test-question counts per chatbot, for the "what to do next" lines. Chatbots
    whose setup hasn't finished have no tables yet and are left out."""
    ready = [b for b in bots if b["state"] not in (*UNFINISHED_STATES, "provisioning")]
    rows, _ = across_bots(ready, journey.progress_select)
    return {r["bot_id"]: r for r in rows}


def _trace(error: Exception) -> str:
    return "".join(traceback.format_exception(type(error), error, error.__traceback__))


def technical_details(where: str, error: Exception) -> None:
    st.caption("For MLOps. You don't need to read this; copy it if you are asked for it.")
    st.code(f"{where} · {type(error).__name__} · {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC\n\n"
            f"{_trace(error)}", language=None)


def report(where: str, error: Exception) -> None:
    """Something failed: say what happened in plain words and what to do now, with the technical
    text folded away for whoever it gets sent to. The trace also goes to the app log."""
    logging.getLogger("factory.app").error("%s failed: %s", where, _trace(error))
    problem = problems.describe(error)
    st.error(f"**Something went wrong {where}.** {problem.what}")
    st.markdown(f"**What to do:** {problem.do}")
    if st.button("Try again", key=f"retry_{where}"):
        forget(settings_too=True)
        st.rerun()
    with st.expander("Show technical details"):
        technical_details(where, error)


def soft(what: str, error: Exception) -> None:
    """One part of a page couldn't be loaded. Say so in a line and carry on with the rest."""
    logging.getLogger("factory.app").warning("%s could not be loaded: %s", what, _trace(error))
    problem = problems.describe(error)
    with st.expander(f"{what} couldn't be loaded just now. The rest of this page is not affected.",
                     icon=":material/warning:"):
        st.markdown(f"{problem.what}\n\n**What to do:** {problem.do}")
        technical_details(what, error)


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
    cfg = json.loads(bot.get("config_json") or "{}")
    return current_user().lower() in {t.lower() for t in cfg.get("testers", [])}


def can_manage(bot: dict) -> bool:
    """Owner person, owner group, reviewer, or MLOps."""
    u = current_user()
    return (is_admin() or u in (bot.get("owner_user"), bot.get("reviewer"))
            or bot.get("owner_group") in user_groups(u))


def my_bots() -> list[dict]:
    return [b for b in all_bots() if can_manage(b)]


def is_owner(bot: dict) -> bool:
    """The owner person, a member of the owner group, or MLOps. Unlike can_manage, a reviewer
    or tester is not an owner."""
    u = current_user()
    return is_admin() or u == bot.get("owner_user") or bot.get("owner_group") in user_groups(u)


def owned_bots() -> list[dict]:
    """Chatbots whose documents this person may see: their own, or all of them for MLOps."""
    return [b for b in all_bots() if is_owner(b)]


ALL_BOTS = "__all__"


def _choose_chatbot(ids: list[str], names: dict[str, str], current: str | None) -> str:
    """The chatbot picker shown at the top of a page, under its title. It sits in the page rather
    than the sidebar so it is always clear which chatbot the page is showing."""
    column, _ = st.columns([2, 3])
    return column.selectbox("Chatbot", ids, index=ids.index(current) if current in ids else 0,
                            format_func=lambda i: names[i],
                            help="Everything below is for the chatbot chosen here.")


def _label(bot: dict) -> str:
    return f"{bot['display_name']} ({STATE_LABEL.get(bot['state'], bot['state'])})"


def pick_owned_bot() -> tuple[str | None, list[dict]]:
    """Picker over the chatbots this person owns, with an "all" entry first. Returns the chosen
    bot_id (ALL_BOTS for all of them, None when they own none) and the owned chatbots."""
    bots = owned_bots()
    if not bots:
        st.info("You don't own any chatbots yet. Create one from **Create a chatbot**.")
        return None, []
    names = {b["bot_id"]: _label(b) for b in bots}
    names[ALL_BOTS] = "All chatbots" if is_admin() else "All my chatbots"
    current = st.session_state.get("docs_bot_id") or st.session_state.get("bot_id")
    choice = _choose_chatbot([ALL_BOTS] + [b["bot_id"] for b in bots], names, current)
    st.session_state["docs_bot_id"] = choice
    if choice != ALL_BOTS:
        st.session_state["bot_id"] = choice
    return choice, bots


def not_ready(bot: dict) -> str:
    """Why a chatbot has nothing to show yet and what to do about it; empty once it is set up."""
    name = bot["display_name"]
    if bot["state"] == "draft":
        return (f"**{name}** isn't set up yet, so there's nothing here for it. Open **Create chatbot**, find it "
                "under **Unfinished chatbots** and press **Continue setup**. Or choose another chatbot above.")
    if bot["state"] == "provision_failed":
        return (f"Setting up **{name}** stopped part-way, so there's nothing here for it yet. Open **Launch** and "
                "press **Try again**. Or choose another chatbot above.")
    if bot["state"] == "provisioning":
        return (f"**{name}** is being set up in the background. This usually takes about 10 minutes. Press "
                "**Refresh** in the menu on the left to check; **Launch** shows the progress.")
    return ""


def pick_bot(during_setup: bool = False) -> tuple[dict, BotConfig] | tuple[None, None]:
    """For pages that only make sense for one chatbot (test questions, launch, monitoring,
    traces): choose it at the top of the page. The choice carries over between pages.

    A chatbot whose setup hasn't finished has no tables yet; pages get (None, None) for it after
    the person is told why, except Launch (`during_setup`), which shows the setup progress."""
    bots = my_bots()
    if not bots:
        st.info("You don't have any chatbots yet. Open **Create chatbot** in the menu on the left to make one.")
        return None, None
    choice = _choose_chatbot([b["bot_id"] for b in bots], {b["bot_id"]: _label(b) for b in bots},
                             st.session_state.get("bot_id"))
    st.session_state["bot_id"] = choice
    st.session_state["docs_bot_id"] = choice
    bot = next(b for b in bots if b["bot_id"] == choice)
    waiting = not_ready(bot)
    if waiting and not (during_setup and bot["state"] != "draft"):
        st.info(waiting)
        return None, None
    return bot, bot_config(bot)


def remember_upload(bot_id: str, report: list[dict]) -> None:
    """Keep the result of an upload so it is still on screen after the page reloads or changes."""
    st.session_state["upload_report"] = {"bot_id": bot_id, "report": report}


def show_upload_report(bot_id: str) -> None:
    """What happened to each file in the last upload for this chatbot: added, already there, or
    not added and why. Stays until dismissed, so a refused file can't go unnoticed."""
    saved = st.session_state.get("upload_report")
    if not saved or saved["bot_id"] != bot_id:
        return
    report = saved["report"]
    added = [r for r in report if r["ok"]]
    already = [r for r in report if not r["ok"] and r["duplicate"]]
    refused = [r for r in report if not r["ok"] and not r["duplicate"]]
    with st.container(border=True):
        st.markdown(f"**Last upload: {len(added)} of {len(report)} file(s) added**")
        if refused:
            st.error(f"{len(refused)} file(s) were **not** added:\n\n"
                     + "\n".join(f"- **{r['name']}**: {' '.join(r['reasons'])}" for r in refused))
        if already:
            st.info(f"{len(already)} file(s) were skipped because the same content is already there: "
                    + ", ".join(r["name"] for r in already))
        if added:
            st.success("Added: " + ", ".join(r["name"] for r in added))
        if st.button("Dismiss", key="dismiss_upload_report"):
            st.session_state.pop("upload_report", None)
            st.rerun()


# What each background job is called on screen, and roughly how long it takes when it has never
# run before (after that, the app goes by how long its recent runs took).
JOB_WHAT = {"evals": ("quality check", 15), "evals_diagnose": ("diagnostic run", 6),
            "promote": ("publishing", 10), "rollback": ("roll back", 5),
            "provision": ("setup", 10), "ingest": ("reading of the documents", 10),
            "ingest_publish": ("update of the test version", 5), "ingest_questions": ("writing of test questions", 5)}


def job_label(kind: str, params: dict) -> str:
    """Which piece of work a job run is for: the ingest and promote jobs each do several."""
    if kind == "ingest":
        return ("ingest_publish" if str(params.get("publish_only")) == "true" else
                "ingest_questions" if str(params.get("golden_only")) == "true" else "ingest")
    if kind == "evals" and str(params.get("diagnose")) in ("true", "paced"):
        return "evals_diagnose"
    return "rollback" if kind == "promote" and params.get("action") == "rollback" else kind


def run_job(kind: str, **params) -> int | None:
    """Start a background job. If an identical run is still waiting to start, that run already
    covers this request and no second one is queued (see factory.jobruns). The run's id is noted
    in the audit log, which is how every page afterwards knows where that run stands."""
    job_id = JOB_IDS.get(kind)
    if not job_id:
        st.error(f"The background job for this step (\"{kind}\") isn't connected to the app, so nothing was "
                 "started. You can't fix this from here: please tell MLOps exactly this message.")
        return None
    params.setdefault("catalog", CATALOG)
    wanted = {k: str(v) for k, v in params.items()}
    try:
        waiting = covered_by(app_client().jobs.list_runs(job_id=int(job_id), active_only=True), wanted)
    except Exception as e:  # noqa: BLE001 - can't tell: start it, which is what used to happen every time
        logging.getLogger("factory.app").info("could not list %s runs (%s); starting a new one", kind,
                                              type(e).__name__)
        waiting = None
    run_id = waiting or app_client().jobs.run_now(job_id=int(job_id), job_parameters=wanted).run_id
    bot_id = wanted.get("bot_id") or ""
    if bot_id and bot_id != "all":
        try:
            cp().audit(current_user(), bot_id, "job_started", job_label(kind, wanted), {"run_id": run_id, **wanted})
        except Exception as e:  # noqa: BLE001 - the job is running; only its progress display is lost
            logging.getLogger("factory.app").warning("run %s started but not noted (%s)", run_id, type(e).__name__)
    return run_id


_FINISHED: dict[int, dict] = {}   # a run that has ended never changes, so it is only looked up once


@st.cache_data(ttl=8, show_spinner=False)
def _live_run(run_id: int) -> dict:
    return jobruns.snapshot(app_client().jobs.get_run(run_id))


def _run_snapshot(run_id: int) -> dict:
    if run_id in _FINISHED:
        return _FINISHED[run_id]
    try:
        snap = _live_run(run_id)
    except Exception as e:  # noqa: BLE001 - the run exists (we started it) but can't be read
        logging.getLogger("factory.app").info("could not read run %s (%s)", run_id, type(e).__name__)
        return {"state": jobruns.UNKNOWN}
    if snap["state"] == jobruns.FAILED and snap.get("task_run_id"):
        # The run itself only says "workload failed, see run output"; the output has the reason.
        try:
            error = getattr(app_client().jobs.get_run_output(snap["task_run_id"]), "error", None)
        except Exception as e:  # noqa: BLE001 - keep Databricks' own short message
            logging.getLogger("factory.app").info("could not read the output of run %s (%s)", run_id, type(e).__name__)
            error = None
        if error:
            snap = {**snap, "message": str(error)[:1000]}
    if snap["state"] in (jobruns.SUCCEEDED, jobruns.FAILED):
        if len(_FINISHED) > 500:
            _FINISHED.clear()
        _FINISHED[run_id] = snap
    return snap


# What it means when a job stops part-way. A job that stops has usually done some of its work,
# so none of these say "nothing was changed".
NO_RESULT = ("No quality result was recorded, so this says nothing about how good your chatbot's answers are. "
             "Answers it had already asked for may show up in MLflow; they are not counted.")
STOPPED = {
    "evals": NO_RESULT,
    "evals_diagnose": "What it printed up to that point is in the report below.",
    "promote": "Publishing did not complete. The versions shown on this page are what is live now.",
    "rollback": "The roll back did not complete. The versions shown on this page are what is live now.",
    "provision": "Setup did not complete. The steps that finished are kept.",
    "ingest": "Documents it had already read are kept; the others have not been read yet.",
    "ingest_publish": "The test version may be only partly updated. The next update finishes the job.",
    "ingest_questions": "Questions it had already written are in the table; the rest were not written.",
}


def what_a_stop_means(label: str, status: jobruns.RunStatus) -> str:
    """What a stopped run left behind, going by which of its tasks stopped."""
    if label != "evals" and status.task in ("evals", "first_eval"):
        return "The update itself finished. " + NO_RESULT
    if status.task == "compare_strategies":
        return ("The update itself finished. The search method was not chosen again, and the quality check that "
                "follows did not run.")
    return STOPPED[label]


def _statuses(rows: list[dict], key: str) -> dict[str, jobruns.RunStatus]:
    """Audit rows of started jobs as {row[key]: where that run stands now}."""
    out = {}
    for r in rows:
        try:
            run_id = int(json.loads(r["detail_json"] or "{}")["run_id"])
        except (KeyError, TypeError, ValueError):
            continue
        out[r[key]] = jobruns.RunStatus(**{"run_id": run_id, "asked_ms": int(float(r.get("asked_ms") or 0)),
                                           "by": r.get("actor") or "", **_run_snapshot(run_id)})
    return out


def job_statuses(bot_id: str) -> dict[str, jobruns.RunStatus]:
    """Where the latest run of each background job stands for a chatbot: {label: status}, for jobs
    started from the app in the last day. Labels are those of job_label()."""
    try:
        rows = sql().query(
            f"""SELECT target, actor, detail_json, unix_timestamp(ts) * 1000 AS asked_ms FROM {settings().fq('audit_log')}
                WHERE bot_id = :b AND action = 'job_started' AND ts >= current_timestamp() - INTERVAL 1 DAY
                QUALIFY row_number() OVER (PARTITION BY target ORDER BY ts DESC) = 1""", {"b": bot_id})
    except Exception as e:  # noqa: BLE001 - knowing a job's progress is a help, never a reason to fail a page
        logging.getLogger("factory.app").info("could not read started jobs (%s)", type(e).__name__)
        return {}
    return _statuses(rows, "target")


# Every update of the test version ends with the search comparison and a quality check (the ingest
# job's second and third tasks), so while one of these is going a check is on its way.
UPDATES = ("ingest_publish", "ingest", "ingest_questions")


def update_in_progress(statuses: dict[str, jobruns.RunStatus]) -> jobruns.RunStatus | None:
    """The update of the test version that is waiting or running, if there is one."""
    return next((statuses[label] for label in UPDATES if label in statuses and statuses[label].active), None)


def checks_in_progress() -> set[str]:
    """The chatbots with a quality check waiting or running right now, started by hand or on its
    way at the end of an update of the test version (for Home's next steps)."""
    try:
        rows = sql().query(
            f"""SELECT bot_id, target, actor, detail_json, unix_timestamp(ts) * 1000 AS asked_ms
                FROM {settings().fq('audit_log')}
                WHERE action = 'job_started' AND target IN ('evals', 'ingest_publish', 'ingest', 'ingest_questions')
                  AND ts >= current_timestamp() - INTERVAL 3 HOURS
                QUALIFY row_number() OVER (PARTITION BY bot_id, target ORDER BY ts DESC) = 1""")
    except Exception:  # noqa: BLE001
        return set()
    busy = set()
    for r in rows:
        if _statuses([r], "bot_id").get(r["bot_id"], jobruns.RunStatus()).active:
            busy.add(r["bot_id"])
    return busy


@st.cache_data(ttl=3600, show_spinner=False)
def _typical_seconds(job_id: str, default: int) -> int:
    return jobruns.typical_seconds(app_client().jobs.list_runs(job_id=int(job_id), completed_only=True, limit=20),
                                   default)


def typical_seconds(label: str) -> int:
    """How long this job has been taking lately, or a first guess when it has no history."""
    default = JOB_WHAT[label][1] * 60
    kind = {"ingest_publish": "ingest", "ingest_questions": "ingest", "rollback": "promote",
            "evals_diagnose": "evals"}.get(label, label)
    try:
        return _typical_seconds(JOB_IDS[kind], default) if JOB_IDS.get(kind) else default
    except Exception:  # noqa: BLE001
        return default


def run_log(status: jobruns.RunStatus) -> str:
    """Everything a finished run printed, or "" when it can't be read."""
    if not status.task_run_id:
        return ""
    try:
        out = app_client().jobs.get_run_output(status.task_run_id)
    except Exception as e:  # noqa: BLE001
        logging.getLogger("factory.app").info("could not read the log of run %s (%s)", status.run_id, type(e).__name__)
        return ""
    return "\n".join(x for x in (getattr(out, "logs", None), getattr(out, "error_trace", None)) if x)


def watch_job(label: str, bot_id: str, quiet: bool = False) -> jobruns.RunStatus:
    """Show where the latest run of a background job stands for this chatbot, and return it.

    While the job is waiting or running there is a progress bar that updates itself every 15
    seconds, and the page reloads once when the job ends, so the result appears without anyone
    pressing anything. A run that stopped with an error says so, with what to do. `quiet` leaves
    out the error (for pages where another part already explains the failure)."""
    status = job_statuses(bot_id).get(label, jobruns.RunStatus())
    what = JOB_WHAT[label][0]
    if status.active:
        @st.fragment(run_every=15)
        def live() -> None:
            now = job_statuses(bot_id).get(label, jobruns.RunStatus())
            if not now.active:      # it ended while this page was open: show what it produced
                forget()
                st.rerun()
            typical = typical_seconds(label)
            st.progress(jobruns.progress(now, time.time() * 1000, typical),
                        text=jobruns.sentence(now, what, time.time() * 1000, typical))
            st.caption("This updates by itself. You can leave the page or close the browser: the job carries on."
                       + (f" [Open the run in Databricks]({now.url})" if now.url else ""))
            with st.popover("Stop this run"):
                st.write(f"This stops the {what} in Databricks. What it has already done is kept, and you can "
                         "start it again afterwards. Use it when a run is taking far longer than usual.")
                if st.button("Yes, stop it", key=f"stop_{label}_{now.run_id}", type="primary"):
                    try:
                        app_client().jobs.cancel_run(now.run_id)
                    except Exception as e:  # noqa: BLE001
                        soft("Stopping the run", e)
                    else:
                        _live_run.clear()
                        forget()
                        st.rerun()
        live()
    elif status.state == jobruns.FAILED and not quiet:
        _, advice = problems.job_stopped(status.message)
        st.error(f"**{jobruns.sentence(status, what, time.time() * 1000, 0)}** {what_a_stop_means(label, status)}")
        st.markdown(f"**What to do:** {advice}" + (f" [Open the run in Databricks]({status.url})" if status.url else ""))
        if status.message:
            st.code(problems.one_line(status.message, 600), language=None)
    elif status.state == jobruns.UNKNOWN and not quiet:
        st.info(jobruns.sentence(status, what, time.time() * 1000, 0) + " Ask MLOps to look at the job in Databricks "
                "if nothing has changed here after half an hour.")
    return status


@st.cache_resource
def start_probe() -> bool:
    """Cheap synthetic probe from the always-on app (OBS-1): no LLM calls."""
    from probe import start
    start(app_client(), settings())
    return True


@st.cache_data(ttl=3600, show_spinner=False)
def _trace_experiment_id() -> str:
    name = f"/Shared/chatbot-factory/{os.environ.get('FACTORY_ENV', 'qa')}/traces"
    return app_client().experiments.get_by_name(name).experiment.experiment_id


def trace_experiment_id() -> str | None:
    """The MLflow experiment that holds the traces. Remembered once found; a miss is asked again."""
    try:
        return _trace_experiment_id()
    except Exception:  # noqa: BLE001 - not deployed yet
        return None


@st.cache_data(ttl=3600, show_spinner=False)
def _job_has_usage_policy(job_id: str) -> bool:
    job = app_client().jobs.get(int(job_id)).settings
    return bool(getattr(job, "usage_policy_id", None) or getattr(job, "budget_policy_id", None))


def has_usage_policy() -> bool | None:
    """Whether the factory's jobs carry a serverless usage policy; None when that can't be read."""
    try:
        return _job_has_usage_policy(JOB_IDS["ingest"]) if JOB_IDS.get("ingest") else None
    except Exception:  # noqa: BLE001 - not allowed to read the job: a person has to say
        return None


@st.cache_data(ttl=3600, show_spinner=False)
def _drift_dashboard_id(table: str) -> str:
    return app_client().quality_monitors.get(table).dashboard_id


def drift_dashboard_id() -> str | None:
    try:
        return _drift_dashboard_id(settings().fq("request_log"))
    except Exception:  # noqa: BLE001 - the monitor isn't set up
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


def safe_traces(bot_id: str | None, limit: int = 300, trace_id: str | None = None) -> list[dict]:
    """load_traces, except that MLflow being unreachable costs the page one section, not all of it."""
    try:
        return load_traces(bot_id, limit, trace_id)
    except Exception as e:  # noqa: BLE001
        soft("The traces from MLflow", e)
        return []
