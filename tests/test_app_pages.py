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
    wh.fail(r"v_alerts_open", RuntimeError("[TABLE_OR_VIEW_NOT_FOUND] v_alerts_open cannot be found"))
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
