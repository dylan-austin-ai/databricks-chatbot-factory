"""The app's pages, run against a pretend workspace.

Needs the app's dependencies: `uv run --extra app pytest`. Without them these tests are skipped,
so the plain `uv run pytest` still runs everything else.

A page is a script that Streamlit runs top to bottom, so the only way to know it works is to run
it. Here every page is run for an owner and for an admin against a warehouse that answers by
pattern, which catches the mistakes unit tests can't: a missing import, a renamed helper, a page
that falls over when a table is empty or a view is missing.
"""
import json
import re
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

pytest.importorskip("streamlit")
pytest.importorskip("pandas")

import streamlit as st  # noqa: E402
from streamlit.delta_generator import DeltaGenerator  # noqa: E402
from streamlit.testing.v1 import AppTest  # noqa: E402

from factory.config import BotConfig  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
CATALOG = "chatbots_test"
OWNER_PAGES = ["home", "create", "documents", "test_questions", "launch", "chat", "monitoring", "traces"]
ADMIN_PAGES = OWNER_PAGES + ["overview", "admin"]


class Warehouse:
    """Answers reads from a list of (pattern, rows), newest first; records reads and writes."""

    parallel = True

    def __init__(self):
        self.answers, self.reads, self.writes, self.broken = [], [], [], []

    def on(self, pattern, rows):
        self.answers.insert(0, (pattern, rows))
        return self

    def fail(self, pattern, error):
        self.broken.append((pattern, error))
        return self

    def _check(self, statement):
        for pattern, error in self.broken:
            if re.search(pattern, statement, re.S):
                raise error

    def query(self, statement, params=None):
        self.reads.append((statement, params or {}))
        self._check(statement)
        for pattern, rows in self.answers:
            if re.search(pattern, statement, re.S):
                return [dict(r) for r in (rows(statement, params or {}) if callable(rows) else rows)]
        return []

    def execute(self, statement, params=None):
        self._check(statement)
        self.writes.append((statement, params or {}))


def bot_row(bot_id="hr_bot", name="HR Bot", state="testing", **extra) -> dict:
    cfg = BotConfig(bot_id=bot_id, display_name=name, purpose="Answers HR questions.", owner_user="ana@corp.com",
                    owner_group="hr", business_function="Other", reviewer="lead@corp.com", allowed_principals=["hr"])
    return {"bot_id": bot_id, "display_name": name, "state": state, "owner_user": "ana@corp.com", "owner_group": "hr",
            "reviewer": "lead@corp.com", "config_json": cfg.to_json(), "config_version": "1",
            "candidate_release_id": None, "live_release_id": None, "deleted_at": None, **extra}


def release(release_id, status, **extra) -> dict:
    return {"release_id": release_id, "bot_id": "hr_bot", "seq": "1", "status": status, "eval_run_id": None,
            "eval_passed": None, "review_requested_at": None, "approved_by": None, "promoted_at": None,
            "doc_count": "4", "smoke_passed": None, **extra}


BOTS = [bot_row("hr_bot", "HR Bot", "live", live_release_id="hr_bot-r0001"), bot_row("it_bot", "IT Bot", "testing"),
        bot_row("new_bot", "New Bot", "draft"), bot_row("slow_bot", "Slow Bot", "provisioning")]


@pytest.fixture
def workspace(monkeypatch):
    """`workspace(bots, admin=False)` wires the app to a pretend Databricks and returns
    (common module, warehouse, jobs started)."""
    monkeypatch.syspath_prepend(str(ROOT / "app"))
    import common

    # Pages are run on their own, outside the menu, so links between pages have nowhere to go.
    monkeypatch.setattr(DeltaGenerator, "page_link", lambda self, page, label="", **k: self.markdown(f"[{label}]"))
    monkeypatch.setattr(st, "page_link", lambda page, label="", **k: st.markdown(f"[{label}]"))
    monkeypatch.setattr(st, "switch_page", lambda page: st.stop())

    def build(bots=BOTS, admin=False, user="ana@corp.com"):
        wh, jobs = Warehouse(), []
        wh.on(r"SELECT \* FROM \S+_platform\.bots\b",
              lambda s, p: [b for b in bots if not p.get("b") or b["bot_id"] == p["b"]])
        st.cache_data.clear()
        st.cache_resource.clear()
        common._FINISHED.clear()
        client = NS(files=NS(list_directory_contents=lambda f: [], upload=lambda *a, **k: None),
                    config=NS(host="https://example.cloud.databricks.com"),
                    experiments=NS(get_by_name=lambda n: NS(experiment=NS(experiment_id="42"))),
                    quality_monitors=NS(get=lambda t: NS(dashboard_id=None)),
                    jobs=NS(list_runs=lambda **k: [], run_now=lambda **k: NS(run_id=1)))
        groups = lambda email: {"hr"} | ({"mlops"} if admin else set())  # noqa: E731
        groups.clear = lambda: None
        overrides = lambda: {"catalog": CATALOG}  # noqa: E731
        overrides.clear = lambda: None
        for name, value in {"_sql": lambda: wh, "CATALOG": CATALOG, "_overrides": overrides,
                            "app_client": lambda: client, "current_user": lambda: user, "user_groups": groups,
                            "start_probe": lambda: True, "load_traces": lambda *a, **k: [],
                            "run_job": lambda kind, **p: jobs.append((kind, p)) or 1}.items():
            monkeypatch.setattr(common, name, value)
        return common, wh, jobs

    return build


def run(page: str, **state) -> AppTest:
    at = AppTest.from_file(str(ROOT / "app" / "pages" / f"{page}.py"), default_timeout=30)
    for key, value in state.items():
        at.session_state[key] = value
    return at.run()


def problems(at: AppTest) -> list[str]:
    return [str(e.value)[:300] for e in at.exception]


@pytest.mark.parametrize("page", OWNER_PAGES)
def test_every_page_renders_for_an_owner(workspace, page):
    workspace()
    assert problems(run(page, bot_id="hr_bot")) == []


@pytest.mark.parametrize("page", ADMIN_PAGES)
def test_every_page_renders_for_an_admin(workspace, page):
    workspace(admin=True)
    assert problems(run(page, bot_id="hr_bot")) == []


@pytest.mark.parametrize("page", OWNER_PAGES)
def test_every_page_renders_when_nothing_exists_yet(workspace, page):
    workspace(bots=[])
    assert problems(run(page)) == []


@pytest.mark.parametrize("section", ["Chatbots", "Lifecycle", "Reports", "Costs", "Answer prompt", "Setup checklist",
                                     "User access", "Alerts", "Audit log"])
def test_every_admin_section_renders(workspace, section):
    workspace(admin=True)
    assert problems(run("admin", admin_section=section)) == []


@pytest.mark.parametrize("page", ["documents", "test_questions", "monitoring", "traces"])
@pytest.mark.parametrize("bot_id, says", [("new_bot", "Continue setup"), ("slow_bot", "being set up")])
def test_a_chatbot_that_is_not_set_up_says_so_instead_of_failing(workspace, page, bot_id, says):
    common, wh, _ = workspace()
    wh.fail(rf"`{bot_id}`", RuntimeError("TABLE_OR_VIEW_NOT_FOUND"))   # its tables don't exist yet
    at = run(page, bot_id=bot_id, docs_bot_id=bot_id, monitoring_scope="One chatbot")
    assert problems(at) == [] and any(says in i.value for i in at.info)


def test_reads_are_remembered_until_something_is_written(workspace):
    common, wh, _ = workspace()
    for _ in range(3):
        common.sql().query("SELECT 1 AS n", {"b": "x"})
    assert len(wh.reads) == 1
    common.sql().query("SELECT 1 AS n", {"b": "y"})            # different parameters: a different read
    assert len(wh.reads) == 2
    common.sql().execute("UPDATE t SET a = 1")                 # any write forgets them all
    common.sql().query("SELECT 1 AS n", {"b": "x"})
    assert len(wh.reads) == 3
    common.sql().execute("INSERT INTO feedback VALUES (1)", keep_reads=True)   # a thumbs-up doesn't
    common.sql().query("SELECT 1 AS n", {"b": "x"})
    assert len(wh.reads) == 3
    for _ in range(2):                                         # services that decide before writing read fresh
        common.fresh_sql().query("SELECT 1 AS n", {"b": "x"})
    assert len(wh.reads) == 5


def test_a_second_look_at_a_page_asks_the_warehouse_nothing(workspace):
    common, wh, _ = workspace()
    at = run("home")
    first = len(wh.reads)
    at.run()
    assert first > 0 and len(wh.reads) == first and problems(at) == []


def test_home_shows_the_next_step_and_survives_a_missing_view(workspace):
    common, wh, _ = workspace()
    wh.on(r"count_if\(status = 'pending_review'", [
        {"bot_id": "it_bot", "reading": "0", "waiting": "3", "flagged": "0", "approved": "1", "questions": "9",
         "questions_ok": "2"}])
    wh.fail(r"_platform\.alerts a LEFT JOIN", RuntimeError("[TABLE_OR_VIEW_NOT_FOUND] alerts cannot be found"))
    at = run("home")
    assert problems(at) == []
    text = " ".join(m.value for m in at.markdown)
    assert "Next: Review your documents" in text and "3 documents are waiting" in text
    assert "Next: Finish setup" in text                       # the draft chatbot
    assert any("Alerts couldn't be loaded" in e.label for e in at.expander)
    assert [b.label for b in at.button].count("Go to Documents") == 1


def test_a_page_that_fails_says_what_to_do_in_plain_words(workspace, monkeypatch):
    common, wh, _ = workspace()
    wh.fail(r"_platform\.bots\b", RuntimeError("TEMPORARILY_UNAVAILABLE: secret-host.internal is starting"))
    monkeypatch.chdir(ROOT / "app")
    at = AppTest.from_file(str(ROOT / "app" / "app.py"), default_timeout=30).run()
    assert problems(at) == []
    assert "Something went wrong on this page" in at.error[0].value and "secret-host" not in at.error[0].value
    assert any("What to do" in m.value and "Try again" in m.value for m in at.markdown)
    assert [e.label for e in at.expander] == ["Show technical details"]
    assert {"Try again", "Refresh"} <= {b.label for b in at.button} | {b.label for b in at.sidebar.button}


def test_across_bots_is_one_statement_and_falls_back_per_chatbot(workspace):
    common, wh, _ = workspace()
    wh.on(r"SELECT 1 AS n", lambda s, p: [{"bot_id": b, "n": "1"} for b in p.values()])
    rows, unread = common.across_bots(BOTS[:2], lambda paths: "SELECT 1 AS n")
    assert len(wh.reads) == 1 and "UNION ALL" in wh.reads[0][0] and unread == []
    assert sorted(r["bot_id"] for r in rows) == ["hr_bot", "it_bot"]
    common.forget()
    wh.fail(r"UNION ALL|:k0 AS bot_id.*`it_bot`", RuntimeError("TABLE_OR_VIEW_NOT_FOUND"))
    rows, unread = common.across_bots(BOTS[:2], lambda paths: f"SELECT 1 AS n FROM {paths.t('manifest')}")
    assert unread == ["it_bot"] and [r["bot_id"] for r in rows] == ["hr_bot"]


def test_a_job_is_not_queued_twice(workspace, monkeypatch):
    common, wh, _ = workspace()
    monkeypatch.undo()                       # the real run_job, with only the workspace client replaced
    monkeypatch.syspath_prepend(str(ROOT / "app"))
    started = []
    queued = NS(run_id=7, status=NS(state=NS(value="QUEUED")), state=None, job_parameters=[
        NS(name="catalog", value=CATALOG, default=None), NS(name="bot_id", value="hr_bot", default=None),
        NS(name="publish_only", value="true", default="false"), NS(name="reparse", value="false", default="false")])
    client = NS(jobs=NS(list_runs=lambda **k: [queued], run_now=lambda **k: started.append(k) or NS(run_id=8)))
    monkeypatch.setattr(common, "app_client", lambda: client)
    monkeypatch.setattr(common, "JOB_IDS", {"ingest": "11"})
    monkeypatch.setattr(common, "CATALOG", CATALOG)
    assert common.run_job("ingest", bot_id="hr_bot", publish_only="true") == 7 and started == []
    assert common.run_job("ingest", bot_id="it_bot", publish_only="true") == 8 and len(started) == 1

    def broken(**k):
        raise RuntimeError("list failed")

    client.jobs.list_runs = broken           # can't tell: start it, as before
    assert common.run_job("ingest", bot_id="hr_bot", publish_only="true") == 8 and len(started) == 2


FAILED = {"metrics": {"hit_at_1": 0.62, "groundedness": 0.8}, "per_case": [],
          "failures": ["hit at 1 is 62%, needs 80%.", "groundedness is 80%, needs 90%."]}


@pytest.mark.parametrize("bot, releases, checks, expect", [
    (bot_row(state="provision_failed"), [], [], "Try again"),
    (bot_row(state="testing", candidate_release_id="hr_bot-r0001"), [release("hr_bot-r0001", "candidate")], [],
     "Run the quality check"),
    (bot_row(state="testing", candidate_release_id="hr_bot-r0001"),
     [release("hr_bot-r0001", "candidate", eval_run_id="e1", eval_passed="false")],
     [{"run_id": "e1", "metrics_json": json.dumps(FAILED), "needs_safety_review": "false"}],
     "Run the quality check again"),
    (bot_row(state="testing", candidate_release_id="hr_bot-r0001"),
     [release("hr_bot-r0001", "candidate", eval_run_id="e1", eval_passed="true")],
     [{"run_id": "e1", "metrics_json": json.dumps({"metrics": {}, "failures": []}), "needs_safety_review": "false"}],
     "Send for approval"),
    (bot_row(state="live", live_release_id="hr_bot-r0002"),
     [release("hr_bot-r0002", "live", eval_passed="true"), release("hr_bot-r0001", "superseded")], [], "Pause"),
])
def test_launch_offers_the_right_next_action_at_each_stage(workspace, bot, releases, checks, expect):
    common, wh, _ = workspace(bots=[bot])
    wh.on(r"_platform\.releases WHERE bot_id", releases)
    wh.on(r"_platform\.eval_runs", checks)
    wh.on(r"provisioning_steps", [{"step": "grant_access", "status": "failed"}])
    at = run("launch", bot_id="hr_bot")
    assert problems(at) == [] and expect in [b.label for b in at.button]
    if expect == "Run the quality check again":   # each failed result comes with what to do about it
        fixes = [m.value for m in at.markdown if m.value.startswith("- **")]
        assert len(fixes) == 2 and all("What to do:" in f for f in fixes)


# --- alerts: calm, plain, and gone when they stop being true -----------------------------------

SPARK_ERROR = ("Provisioning failed: [AI_FUNCTION_COMPILATION_ERROR] An error occurred while compiling the AI function\n"
               "== SQL (line 5, position 13) ==\nSELECT ai_parse_document(content, map('a', b))\n-------------^^^")


def alert_row(alert_id, bot_id, kind, message, state, age_hours, severity="high") -> dict:
    return {"alert_id": alert_id, "ts": "2026-10-08T14:03:22.000Z", "bot_id": bot_id, "kind": kind,
            "severity": severity, "message": message, "resolved": "false", "dedupe_key": None, "required": "true",
            "bot_state": state, "age_hours": str(age_hours), "this_month": "true"}


ALERTS = [alert_row("a1", "it_bot", "provision_failed", SPARK_ERROR, "testing", 70),
          alert_row("a2", "it_bot", "provision_failed", SPARK_ERROR, "testing", 71),
          alert_row("a3", "it_bot", "alert_setup_failed", "Email alerts could not be set up automatically "
                    "(AlertV2.__init__() got an unexpected keyword argument 'custom_subject')", "testing", 70, "medium"),
          alert_row("a4", "hr_bot", "expiry", '"Rates 2026.pdf" expires on Oct 23, 2026 (12 days).', "live", 5, "medium"),
          alert_row("a5", "hr_bot", "quality_drop", "groundedness is 70% over 7 days (needs 90%).", "live", 3),
          alert_row("a6", "hr_bot", "quality_drop", "groundedness is 72% over 7 days (needs 90%).", "live", 27)]


def page_text(at: AppTest) -> str:
    return " ".join(x.value for kind in (at.markdown, at.caption, at.error, at.warning, at.info) for x in kind)


def test_home_shows_alerts_calmly_and_only_the_ones_still_true(workspace):
    common, wh, _ = workspace()
    wh.on(r"_platform\.alerts a LEFT JOIN", ALERTS)
    at = run("home")
    text = page_text(at)
    assert problems(at) == []
    # the setup failure is long over (the chatbot is in testing): not shown, and no raw error text anywhere
    assert "Setup stopped" not in text and "AI_FUNCTION_COMPILATION_ERROR" not in text and "== SQL" not in text
    assert "AlertV2" not in text and "custom_subject" not in text
    # the owner's own: one line each, repeats folded, with what to do
    assert text.count("Document expiring") == 1 and "Rates 2026" in text
    assert text.count("Quality dropped") == 1 and "raised 2 times" in text and "What to do:" in text
    # what the owner can't act on is one quiet line, not a warning
    assert "MLOps has been told about 1 other issue(s)" in text and "email alerts not set up" in text
    assert "Open them" not in [b.label for b in at.button]


def test_home_points_an_admin_to_the_alerts_that_are_theirs(workspace):
    common, wh, _ = workspace(admin=True)
    wh.on(r"_platform\.alerts a LEFT JOIN", ALERTS + [alert_row("a7", None, "availability", "97.0%", None, 1)])
    at = run("home")
    text = page_text(at)
    assert problems(at) == [] and "2 alert(s) for MLOps to look at" in text and "AlertV2" not in text
    assert "Open them" in [b.label for b in at.button]


def test_admin_alerts_are_grouped_readable_and_stale_ones_are_closed(workspace):
    common, wh, _ = workspace(admin=True)
    wh.on(r"_platform\.alerts a LEFT JOIN", lambda s, p: [a for a in ALERTS if a["alert_id"] not in closed])
    closed: set = set()
    at = run("admin", admin_section="Alerts")
    assert problems(at) == []
    update = [(s, p) for s, p in wh.writes if s.startswith("UPDATE") and "alert_id IN" in s]
    assert sorted(update[0][1].values()) == ["a1", "a2"]                  # their setup finished long ago
    text = page_text(at)
    assert "2 older alert(s) whose cause had gone were closed" in text
    assert "Email alerts not set up" in text and "Try setting them up again" in text      # advice for MLOps
    # the recorded message is shown as typed in a code block, never as formatted text
    assert any("custom_subject" in c.value for c in at.code) and "custom_subject" not in text
    labels = [b.label for b in at.button]
    assert labels.count("Resolve") == 3 and "Try setting them up again" in labels


# --- documents: the chooser, identical copies and a failed delete ------------------------------

def manifest_row(doc_id, name, content_hash, uploaded_at, status="approved") -> dict:
    return {"doc_id": doc_id, "doc_name": name, "doc_version": "1", "status": status, "readability": "green",
            "content_hash": content_hash, "uploaded_at": uploaded_at, "uploaded_by": "ana@corp.com", "page_count": "3",
            "no_expiry": "true", "expires_at": None, "qa_json": "{}", "parser": "ai_parse_document", "flag_reason": None,
            "chunk_count": "2", "doc_owner": None, "page_range": None, "is_active": "true", "file_ext": "pdf",
            "file_path": f"/Volumes/chatbots_test/hr_bot/docs/files/{doc_id}.pdf"}


COPIES = [manifest_row("a", "Guide.pdf", "h1", "2026-10-01 09:00:00"),
          manifest_row("b", "Guide (1).pdf", "h1", "2026-10-05 09:00:00", "pending_review"),
          manifest_row("c", "Leave policy.pdf", "h2", "2026-10-02 09:00:00")]


def test_documents_offers_three_clear_choices(workspace):
    common, wh, _ = workspace(bots=[bot_row()])
    wh.on(r"`manifest`", COPIES)
    at = run("documents", docs_bot_id="hr_bot")
    assert problems(at) == []
    assert [b.label for b in at.button] == ["Add documents", "Curate documents", "Review a document"]
    assert any("What would you like to do?" in m.value for m in at.markdown)
    at.button(key="docs_action_curate").click().run()
    assert at.session_state["docs_section"] == "Curate documents" and "Curate documents" in [x.value for x in at.subheader]


def test_identical_copies_are_archived_in_one_click_and_only_offered_when_there_are_some(workspace):
    common, wh, jobs = workspace(bots=[bot_row()])
    wh.on(r"`manifest`", COPIES)
    at = run("documents", docs_bot_id="hr_bot", docs_section="Curate documents")
    assert problems(at) == [] and "Archive the 1 older copy" in [b.label for b in at.button]
    assert "approve the copy that is kept" in page_text(at)                 # the older copy was the approved one
    at.button(key="archive_identical").click().run()
    archive = [(s, p) for s, p in wh.writes if s.startswith("UPDATE") and "status = 'archived'" in s]
    assert archive and archive[0][1] == {"d0": "a"} and ("ingest", {"bot_id": "hr_bot", "publish_only": "true"}) in jobs
    assert any("Archived the older copy of 1 document(s)." in x.value for x in at.success)

    common, wh, _ = workspace(bots=[bot_row()])
    wh.on(r"`manifest`", [COPIES[0], COPIES[2]])                            # similar files, but no identical ones
    at = run("documents", docs_bot_id="hr_bot", docs_section="Curate documents")
    assert problems(at) == [] and not [b for b in at.button if b.label.startswith("Archive the 1 older")]


def test_a_failed_delete_says_what_to_do_and_keeps_the_details(workspace):
    common, wh, _ = workspace(bots=[bot_row()], admin=True)
    wh.on(r"`manifest`", COPIES)
    wh.fail(r"DELETE FROM \S+`chunked`", RuntimeError("[DELTA_CONCURRENT_APPEND] a concurrent update added files"))
    at = AppTest.from_file(str(ROOT / "app" / "pages" / "documents.py"), default_timeout=30)
    at.session_state["docs_bot_id"], at.session_state["docs_section"] = "hr_bot", "Curate documents"
    at.session_state["manage_In use or waiting_0"] = {"edited_rows": {0: {"Select": True}}, "added_rows": [],
                                                       "deleted_rows": []}
    at.run()
    at.text_input(key="manage_why").set_value("Duplicate uploads").run()
    at.checkbox(key="manage_sure").check().run()
    at.button(key="manage_delete").click().run()
    assert problems(at) == []
    assert any("That didn't work" in e.value and "same moment" in e.value for e in at.error)
    assert any("What to do:" in m.value and "deleting it again finishes the job" in m.value for m in at.markdown)
    assert "Show technical details" in [e.label for e in at.expander]
    assert not [x for x in at.success if "Deleted" in x.value]



def test_stored_text_is_shown_as_plain_words(workspace):
    workspace()
    import ui

    out = ui.plain("used $950 of $1,000\n== SQL (line 5) ==\n-----^^^ <b>x</b> # big *bold* `code`")
    assert "\n" not in out and "<b>" not in out and "&lt;b&gt;" in out
    for defused in ("\\$950", "\\=\\=", "\\-\\-\\-", "\\#", "\\*bold\\*", "\\`code\\`"):
        assert defused in out
    assert ui.plain(None) == ""


# --- the setup checklist: an empty box always means something is still to do ---------------------

def test_setup_checklist_separates_what_the_app_checked_from_what_a_person_confirms(workspace):
    common, wh, _ = workspace(admin=True)
    at = run("admin", admin_section="Setup checklist")
    text = page_text(at)
    assert problems(at) == []
    assert "Checked by the app" in text and "Only a person can confirm these" in text
    assert "0 of 10 done" in text and "To do" in text and "What to do:" in text
    assert "Answer service inference table" in text and "Save settings" in text          # exactly where to enter it
    boxes = {c.key: c.value for c in at.checkbox}
    assert boxes == {"setup_usage_policy": False, "setup_user_authorisation": False, "setup_review_queues": False,
                     "setup_embed_dashboards": False, "setup_account_budget": False, "setup_app_scaling": False}

    at.checkbox(key="setup_review_queues").check().run()                # the tick is saved with who and when
    saved = [p for s, p in wh.writes if "platform_settings" in s]
    assert len(saved) == 1 and saved[0]["k"] == "setup_confirmed"      # written once, on the click
    tick = json.loads(saved[0]["v"])["review_queues"]
    assert tick["by"] == "ana@corp.com" and re.fullmatch(r"\d{4}-\d{2}-\d{2}", tick["on"])


def test_setup_checklist_shows_done_for_what_is_recorded_and_there(workspace, monkeypatch):
    common, wh, _ = workspace(admin=True)
    home = f"{CATALOG}._platform"
    saved = {"catalog": CATALOG, "unity_gateway.inference_table": f"{home}.gw_answer_payload",
             "unity_gateway.evaluator_inference_table": f"{home}.gw_eval_payload",
             "setup_confirmed": {"review_queues": {"by": "lead@corp.com", "on": "2026-10-01"}}}
    overrides = lambda: saved  # noqa: E731
    overrides.clear = lambda: None
    monkeypatch.setattr(common, "_overrides", overrides)
    monkeypatch.setattr(common, "has_usage_policy", lambda: True)
    wh.on(r"information_schema\.tables", [{"name": "gw_answer_payload"}])
    at = run("admin", admin_section="Setup checklist")
    text = page_text(at)
    assert problems(at) == [] and "3 of 10 done, 4 still to do, 3 for you to confirm" in text
    assert "can't find a table called `chatbots_test._platform.gw_eval_payload`" in text
    assert {c.key: c.value for c in at.checkbox}["setup_review_queues"] is True
    assert "Confirmed by lead@corp.com on 2026-10-01" in text
    assert "setup_usage_policy" not in {c.key for c in at.checkbox}        # the app could see that one for itself


def test_a_table_name_that_is_not_a_plain_full_name_is_refused(workspace):
    common, wh, _ = workspace(admin=True)
    at = run("admin")
    at.text_input(key="unity_gateway.inference_table").set_value("qa._platform.t; DROP TABLE x")
    [b for b in at.button if b.label == "Save settings"][0].click().run()     # a form sends its fields on submit
    assert problems(at) == []
    assert any("catalog.schema.table" in e.value for e in at.error)
    assert not [s for s, _ in wh.writes if "platform_settings" in s]



# --- background jobs: the page shows the run itself, not a note that a button was pressed --------

def job_run(state, started_ago_s=360, ended_ago_s=0, result=None, message=None):
    """A run the way the Jobs API returns it (newer shape)."""
    import time

    now = int(time.time() * 1000)
    details = NS(code=NS(value=result), message=message) if result else None
    return NS(status=NS(state=NS(value=state), termination_details=details), state=None,
              start_time=now - started_ago_s * 1000, end_time=now - ended_ago_s * 1000 if ended_ago_s else 0,
              run_page_url="https://example.cloud.databricks.com/jobs/1/runs/7")


def started_job(common, wh, label, run, bot_id="hr_bot", minutes_ago=7):
    """Pretend `label` was started from the app for `bot_id` and Databricks now reports `run`."""
    import time

    wh.on(r"action = 'job_started'", [{"target": label, "bot_id": bot_id, "actor": "ana@corp.com",
                                         "detail_json": '{"run_id": 7}',
                                         "asked_ms": str(int(time.time() * 1000) - minutes_ago * 60000)}])
    common.app_client().jobs.get_run = lambda run_id: run


CHECKABLE = bot_row(state="testing", candidate_release_id="hr_bot-r0001")


def test_a_running_quality_check_shows_progress_and_locks_its_button(workspace):
    common, wh, _ = workspace(bots=[CHECKABLE])
    wh.on(r"_platform\.releases WHERE bot_id", [release("hr_bot-r0001", "candidate")])
    started_job(common, wh, "evals", job_run("RUNNING"))
    at = run("launch", bot_id="hr_bot")
    assert problems(at) == []
    bar = at.get("progress")[0].proto
    assert bar.text == "The quality check is running: 6 minutes so far. It usually takes about 15 minutes."
    assert 35 <= bar.value <= 45                                      # six of about fifteen minutes
    assert ("Quality check running…", True) in [(b.label, b.disabled) for b in at.button]
    assert "Run the quality check" not in [b.label for b in at.button]
    text = page_text(at)
    assert "You can leave the page" in text and "runs/7" in text      # and a link to the run for MLOps
    assert "The quality check is running. **Launch** shows how far along it is" in text    # not "Your turn"


def test_a_quality_check_waiting_to_start_says_so(workspace):
    common, wh, _ = workspace(bots=[CHECKABLE])
    wh.on(r"_platform\.releases WHERE bot_id", [release("hr_bot-r0001", "candidate")])
    started_job(common, wh, "evals", job_run("QUEUED", started_ago_s=0), minutes_ago=2)
    at = run("launch", bot_id="hr_bot")
    bar = at.get("progress")[0].proto
    assert problems(at) == [] and bar.value == 0 and "waiting its turn (2 minutes so far)" in bar.text
    assert "started by ana@corp.com" in bar.text
    assert ("Quality check running…", True) in [(b.label, b.disabled) for b in at.button]


def test_a_quality_check_that_stopped_says_so_and_can_be_started_again(workspace):
    common, wh, jobs = workspace(bots=[CHECKABLE])
    wh.on(r"_platform\.releases WHERE bot_id", [release("hr_bot-r0001", "candidate")])
    started_job(common, wh, "evals", job_run("TERMINATED", ended_ago_s=120, result="RUN_EXECUTION_ERROR",
                                             message="Task evals failed:\nModuleNotFoundError: No module named 'x'"))
    at = run("launch", bot_id="hr_bot")
    assert problems(at) == [] and not at.get("progress")
    assert any("The last quality check stopped with an error 2 minutes ago" in e.value for e in at.error)
    assert any("What to do:" in m.value and "runs/7" in m.value for m in at.markdown)
    assert any("ModuleNotFoundError" in c.value and "\n" not in c.value for c in at.code)   # one line, as typed
    at.button(key="run_check").click().run()                           # and it can simply be started again
    assert ("evals", {"bot_id": "hr_bot", "trigger": "manual", "channel": "candidate"}) in jobs


def test_a_check_that_finished_without_a_result_is_explained(workspace):
    common, wh, _ = workspace(bots=[CHECKABLE])
    wh.on(r"_platform\.releases WHERE bot_id", [release("hr_bot-r0001", "candidate")])
    started_job(common, wh, "evals", job_run("TERMINATED", ended_ago_s=300, result="SUCCESS"))
    at = run("launch", bot_id="hr_bot")
    assert problems(at) == []
    assert any("finished earlier but left no result for this version" in w.value for w in at.warning)
    assert ("Run the quality check", False) in [(b.label, b.disabled) for b in at.button]


def test_test_questions_locks_the_check_and_the_question_writer_while_they_run(workspace):
    common, wh, _ = workspace(bots=[CHECKABLE])
    started_job(common, wh, "evals", job_run("RUNNING"))
    at = run("test_questions", bot_id="hr_bot")
    assert problems(at) == [] and ("Quality check running…", True) in [(b.label, b.disabled) for b in at.button]
    assert "quality check is running" in at.get("progress")[0].proto.text

    common, wh, _ = workspace(bots=[CHECKABLE])
    started_job(common, wh, "ingest_questions", job_run("RUNNING", started_ago_s=60))
    at = run("test_questions", bot_id="hr_bot")
    assert problems(at) == [] and ("Writing questions…", True) in [(b.label, b.disabled) for b in at.button]
    assert "writing of test questions is running: 1 minute so far" in at.get("progress")[0].proto.text


def test_home_does_not_say_your_turn_while_a_check_is_running(workspace):
    common, wh, _ = workspace(bots=[CHECKABLE])
    wh.on(r"count_if\(status = 'pending_review'", [
        {"bot_id": "hr_bot", "reading": "0", "waiting": "0", "flagged": "0", "approved": "5", "questions": "60",
         "questions_ok": "55"}])
    wh.on(r"JOIN \S+_platform\.bots b ON r\.release_id", [
        {"bot_id": "hr_bot", "release_id": "hr_bot-r0001", "eval_run_id": None, "eval_passed": None,
         "review_requested_at": None}])
    started_job(common, wh, "evals", job_run("RUNNING"))
    text = page_text(run("home"))
    assert "Next: Pass the quality check" in text and "The quality check is running" in text
    assert "Nothing to do yet" in text and "press **Run the quality check**" not in text


def test_starting_a_job_notes_its_run_so_every_page_can_follow_it(workspace, monkeypatch):
    common, wh, _ = workspace()
    monkeypatch.undo()                       # the real run_job again
    monkeypatch.syspath_prepend(str(ROOT / "app"))
    started = []
    client = NS(jobs=NS(list_runs=lambda **k: [], run_now=lambda **k: started.append(k) or NS(run_id=41)), files=NS())
    for name, value in {"_sql": lambda: wh, "app_client": lambda: client, "current_user": lambda: "ana@corp.com",
                        "JOB_IDS": {"ingest": "11", "promote": "12"}, "CATALOG": CATALOG,
                        "settings": lambda: common.PlatformSettings.load({"catalog": CATALOG})}.items():
        monkeypatch.setattr(common, name, value)
    assert common.run_job("ingest", bot_id="hr_bot", publish_only="true") == 41
    assert common.run_job("promote", bot_id="hr_bot", action="rollback", reason="x") == 41
    noted = [p for s, p in wh.writes if "audit_log" in s and p["action"] == "job_started"]
    assert [(p["bot"], p["target"], json.loads(p["detail"])["run_id"]) for p in noted] == [
        ("hr_bot", "ingest_publish", 41), ("hr_bot", "rollback", 41)]
    assert noted[0]["actor"] == "ana@corp.com"
    assert common.run_job("ingest", bot_id="all") == 41 and len(noted) == 2      # platform-wide runs aren't noted


def test_launch_says_when_the_search_method_was_chosen_and_that_it_is_not_the_quality_check(workspace):
    common, wh, _ = workspace(bots=[CHECKABLE])
    wh.on(r"_platform\.releases WHERE bot_id", [release("hr_bot-r0001", "candidate")])
    wh.on(r"strategy_results", [{"explanation": 'We tested 5 ways of searching your documents. "Meaning + keyword '
                                                'search" worked best.', "seconds_ago": str(3 * 86400 + 40)}])
    at = run("launch", bot_id="hr_bot")
    assert problems(at) == []
    assert any(i.value.startswith("**Search method, chosen automatically 3 days ago.** We tested 5 ways")
               for i in at.info)
    text = page_text(at)
    assert "This is not the quality check" in text and "The chatbot doesn't answer anything in this step" in text
    assert "runs by itself at the end of every update of the test version once 50 questions are approved" in text


def test_an_update_of_the_test_version_shows_its_stage_and_locks_the_manual_check(workspace):
    common, wh, jobs = workspace(bots=[CHECKABLE])
    wh.on(r"_platform\.releases WHERE bot_id", [release("hr_bot-r0001", "candidate")])
    update = job_run("RUNNING", started_ago_s=480)
    task = lambda key, state, code=None: NS(task_key=key, state=None, status=NS(   # noqa: E731
        state=NS(value=state), termination_details=NS(code=NS(value=code), message=None) if code else None))
    update.tasks = [task("ingest", "TERMINATED", "SUCCESS"), task("compare_strategies", "TERMINATED", "SUCCESS"),
                    task("evals", "RUNNING")]
    started_job(common, wh, "ingest_publish", update)
    at = run("launch", bot_id="hr_bot")
    assert problems(at) == []
    assert "update of the test version is running: step 3 of 3 (the quality check), 8 minutes so far" in \
        at.get("progress")[0].proto.text
    assert ("Runs when the update finishes", True) in [(b.label, b.disabled) for b in at.button]
    assert "A quality check runs by itself as the last step of every update" in page_text(at)
    assert "The quality check is running. **Launch** shows how far along it is" in page_text(at)   # not "Your turn"

    at = run("test_questions", bot_id="hr_bot")                       # the same lock on the questions page
    assert problems(at) == [] and ("Runs when the update finishes", True) in [(b.label, b.disabled) for b in at.button]
    assert jobs == []


def test_a_check_stopped_by_the_jobs_setup_shows_the_real_reason_and_does_not_claim_nothing_changed(workspace):
    common, wh, _ = workspace(bots=[CHECKABLE])
    wh.on(r"_platform\.releases WHERE bot_id", [release("hr_bot-r0001", "candidate")])
    stopped = job_run("TERMINATED", ended_ago_s=30, result="RUN_EXECUTION_ERROR",
                      message="Task evals failed with message: Workload failed, see run output for details.")
    stopped.tasks = [NS(task_key="evals", run_id=99, state=None, status=stopped.status)]
    started_job(common, wh, "evals", stopped)
    asked = []
    common.app_client().jobs.get_run_output = lambda run_id: asked.append(run_id) or NS(
        error="MlflowException: SQL warehouse ID is required for accessing traces in UC tables. Please set the "
              "MLFLOW_TRACING_SQL_WAREHOUSE_ID environment variable.", error_trace="Traceback ...")
    at = run("launch", bot_id="hr_bot")
    assert problems(at) == [] and asked == [99]                      # the failed task's own output
    error = at.error[0].value
    assert "The last quality check stopped with an error" in error and "Nothing was changed" not in error
    assert "No quality result was recorded" in error and "says nothing about how good your chatbot's answers" in error
    assert any("SQL warehouse ID is required" in c.value for c in at.code)          # not "see run output"
    advice = [m.value for m in at.markdown if m.value.startswith("**What to do:**")][0]
    assert "fault in how the job is set up" in advice and "won't help" in advice and "runs/7" in advice


def test_an_update_that_stopped_at_its_quality_check_says_the_update_itself_finished(workspace):
    common, wh, _ = workspace(bots=[CHECKABLE])
    wh.on(r"_platform\.releases WHERE bot_id", [release("hr_bot-r0001", "candidate")])
    ok = NS(state=NS(value="TERMINATED"), termination_details=NS(code=NS(value="SUCCESS"), message=None))
    bad = NS(state=NS(value="TERMINATED"), termination_details=NS(code=NS(value="RUN_EXECUTION_ERROR"), message=None))
    stopped = job_run("TERMINATED", ended_ago_s=60, result="RUN_EXECUTION_ERROR", message="Task evals failed")
    stopped.tasks = [NS(task_key="ingest", run_id=1, state=None, status=ok),
                     NS(task_key="compare_strategies", run_id=2, state=None, status=ok),
                     NS(task_key="evals", run_id=3, state=None, status=bad)]
    started_job(common, wh, "ingest_publish", stopped)
    at = run("launch", bot_id="hr_bot")
    error = [e.value for e in at.error if "update of the test version" in e.value][0]
    assert problems(at) == [] and "at step 3 of 3 (the quality check)" in error
    assert "The update itself finished. No quality result was recorded" in error


def test_a_run_can_be_stopped_from_the_page(workspace):
    common, wh, _ = workspace(bots=[CHECKABLE])
    wh.on(r"_platform\.releases WHERE bot_id", [release("hr_bot-r0001", "candidate")])
    started_job(common, wh, "evals", job_run("RUNNING", started_ago_s=3600))
    stopped = []
    common.app_client().jobs.cancel_run = lambda run_id: stopped.append(run_id)
    at = run("launch", bot_id="hr_bot")
    assert problems(at) == [] and "This one is taking longer than usual" in at.get("progress")[0].proto.text
    at.button(key="stop_evals_7").click().run()
    assert stopped == [7]


def test_the_diagnostic_run_is_started_from_admin_and_its_report_is_shown(workspace):
    common, wh, jobs = workspace(bots=[CHECKABLE], admin=True)
    at = run("admin", admin_section="Setup checklist")
    assert problems(at) == [] and "Diagnose the quality check" in page_text(at)
    at.button(key="run_diagnostic").click().run()
    assert ("evals", {"bot_id": "hr_bot", "trigger": "diagnose", "channel": "candidate", "limit": "3",
                      "diagnose": "true"}) in jobs

    common, wh, _ = workspace(bots=[CHECKABLE], admin=True)
    finished = job_run("TERMINATED", ended_ago_s=60, result="SUCCESS")
    finished.tasks = [NS(task_key="evals", run_id=77, state=None, status=finished.status)]
    started_job(common, wh, "evals_diagnose", finished)
    common.app_client().jobs.get_run_output = lambda run_id: NS(error=None, error_trace=None, logs=(
        "some other line\nDIAG: 1. the chatbot answers one question: ok in 4.2s\n"
        "DIAG: 4. that trace is read back by its id: STILL WAITING after 120s (this is where it stalls)\n"
        "Thread 0x00007f (most recent call first):\n  File \"databricks/sql/client.py\", line 1 in execute\n"))
    at = run("admin", admin_section="Setup checklist")
    assert problems(at) == []
    report = [c.value for c in at.code if "DIAG:" in c.value]
    assert report[0].splitlines() == [
        "DIAG: 1. the chatbot answers one question: ok in 4.2s",
        "DIAG: 4. that trace is read back by its id: STILL WAITING after 120s (this is where it stalls)"]
    assert any("Thread 0x" in c.value for c in at.code)               # the full output, thread dump included
    assert "STILL WAITING" in page_text(at)                           # how to read it


def test_launch_says_how_many_questions_a_result_rests_on(workspace):
    common, wh, _ = workspace(bots=[CHECKABLE])
    wh.on(r"_platform\.releases WHERE bot_id", [release("hr_bot-r0001", "candidate", eval_run_id="e1", eval_passed="true")])
    wh.on(r"_platform\.eval_runs", [{"run_id": "e1", "needs_safety_review": "false", "metrics_json": json.dumps(
        {"metrics": {"correctness": 0.95}, "failures": [], "n_cases": 50, "n_approved": 312})}])
    at = run("launch", bot_id="hr_bot")
    assert problems(at) == [] and "Checked on 50 of your 312 approved questions" in page_text(at)


def test_the_diagnostic_can_run_a_batch_at_the_checks_normal_pace(workspace):
    common, wh, jobs = workspace(bots=[CHECKABLE], admin=True)
    at = run("admin", admin_section="Setup checklist")
    at.number_input(key="diagnose_size").set_value(10).run()
    at.radio(key="diagnose_pace").set_value("paced").run()
    at.button(key="run_diagnostic").click().run()
    assert problems(at) == []
    assert ("evals", {"bot_id": "hr_bot", "trigger": "diagnose", "channel": "candidate", "limit": "10",
                      "diagnose": "paced"}) in jobs
    assert common.job_label("evals", {"diagnose": "paced"}) == "evals_diagnose"
