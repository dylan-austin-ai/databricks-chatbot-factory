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
