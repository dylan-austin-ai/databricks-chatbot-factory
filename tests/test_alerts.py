"""Alerts: short at the source, plain on the page, and closed when they stop being true."""
from conftest import FakeSql
from factory import monitoring, problems
from factory.controlplane import ControlPlane

SPARK_ERROR = """[AI_FUNCTION_COMPILATION_ERROR] An error occurred while compiling the AI function: INVALID_ARGUMENT
Call to `ai_parse_document` is invalid because `options` requires a literal MAP<STRING, STRING>. SQLSTATE: 42883
== SQL (line 5, position 13) ==
SELECT ai_parse_document(content, map('imageOutputPath', path)) FROM files
------------^^^
JVM stacktrace: org.apache.spark.sql.AnalysisException at ..."""


def test_an_error_becomes_one_short_line():
    line = problems.brief(RuntimeError(SPARK_ERROR))
    assert line.startswith("RuntimeError: [AI_FUNCTION_COMPILATION_ERROR]") and "\n" not in line
    assert len(line) <= 200 and "==" not in line and "JVM" not in line
    assert problems.brief(TimeoutError()) == "TimeoutError"
    assert problems.brief(ValueError("x" * 500), limit=50).endswith("…") and len(problems.brief(ValueError("x" * 500), 50)) == 50
    assert problems.one_line("a\n\n   b\tc") == "a b c"


def test_machine_messages_are_replaced_by_a_plain_sentence():
    assert problems.alert_text("provision_failed", SPARK_ERROR) == "Setting up this chatbot stopped part-way."
    assert "AlertV2" not in problems.alert_text("alert_setup_failed", "AlertV2.__init__() got an unexpected keyword")
    readable = '"Rates 2026.pdf" expires on Oct 23, 2026 (12 days).'
    assert problems.alert_text("expiry", readable) == readable
    assert "\n" not in problems.alert_text("quality_drop", "line one\nline two")


def test_repeats_are_folded_and_split_by_who_can_act():
    alerts = [{"alert_id": "3", "bot_id": "hr", "kind": "alert_setup_failed", "severity": "medium", "message": "x"},
              {"alert_id": "2", "bot_id": "hr", "kind": "provision_failed", "severity": "high", "message": SPARK_ERROR},
              {"alert_id": "1", "bot_id": "hr", "kind": "provision_failed", "severity": "high", "message": SPARK_ERROR},
              {"alert_id": "0", "bot_id": None, "kind": "availability", "severity": "high", "message": "97%"}]
    groups = problems.group_alerts(alerts)
    assert [(g["bot_id"], g["kind"], g["count"]) for g in groups] == [
        ("hr", "alert_setup_failed", 1), ("hr", "provision_failed", 2), (None, "availability", 1)]
    setup, failed, platform = groups
    assert failed["label"] == "Setup stopped" and failed["for_owner"] and failed["latest"]["alert_id"] == "2"
    assert setup["label"] == "Email alerts not set up" and not setup["for_owner"]
    assert "Try setting them up again" in setup["mlops_advice"] and "nothing for you to do" in setup["advice"]
    assert not platform["for_owner"] and "serving endpoint" in platform["mlops_advice"]


def test_an_alert_stops_counting_when_it_is_no_longer_true():
    current = monitoring.alert_is_current
    # setup failures follow the chatbot's state, whatever their age
    assert current("provision_failed", "provision_failed", 500) and not current("provision_failed", "testing", 0.1)
    # checks that run every 15 minutes: a day without a repeat means it has stopped
    assert current("latency", "live", 23) and not current("latency", "live", 25)
    assert current("quality_drop", "live", 47) and not current("quality_drop", "live", 49)        # daily checks: two days
    assert current("budget", "live", 600, this_month=True) and not current("budget", "live", 2, this_month=False)
    assert current("expiry", "live", 30 * 24) and not current("expiry", "live", 32 * 24)
    assert current("alert_setup_failed", "testing", 20 * 24)                  # until the setup works
    assert not current("latency", "archived", 1) and not current("expiry", "deleted", 1)   # nobody can act
    assert current("availability", None, 1, has_bot=False)                    # a platform alert has no chatbot


def _row(alert_id, kind, state, age, bot="hr", month="true"):
    return {"alert_id": alert_id, "bot_id": bot, "kind": kind, "bot_state": state, "age_hours": str(age),
            "this_month": month, "severity": "high", "message": "m"}


ROWS = [_row("a1", "provision_failed", "testing", 60), _row("a2", "provision_failed", "testing", 61),
        _row("a3", "alert_setup_failed", "testing", 60), _row("a4", "latency", "live", 30, bot="it"),
        _row("a5", "expiry", "live", 100, bot="it"), _row("a6", "availability", None, 2, bot=None)]


def test_open_alerts_are_only_the_ones_still_true(settings):
    sql = FakeSql([(r"FROM \S+alerts a LEFT JOIN", ROWS)])
    cp = ControlPlane(sql, settings)
    assert [a["alert_id"] for a in cp.open_alerts()] == ["a3", "a5", "a6"]
    statement = sql.statements[0][0]
    assert "unix_timestamp(current_timestamp()) - unix_timestamp(a.ts)" in statement and "b.state AS bot_state" in statement


def test_clearing_closes_exactly_the_stale_ones(settings):
    sql = FakeSql([(r"FROM \S+alerts a LEFT JOIN", ROWS)])
    assert ControlPlane(sql, settings).clear_stale_alerts() == 3
    update, params = [(s, p) for s, p in sql.statements if s.startswith("UPDATE")][0]
    assert update.endswith("WHERE alert_id IN (:a0, :a1, :a2)") and sorted(params.values()) == ["a1", "a2", "a4"]
    assert sql.find(r"INSERT INTO \S+audit_log")                              # one audit row for the sweep
    quiet = FakeSql([(r"FROM \S+alerts a LEFT JOIN", ROWS[2:3])])
    assert ControlPlane(quiet, settings).clear_stale_alerts() == 0 and len(quiet.statements) == 1


def test_a_repeated_failure_replaces_its_alert_instead_of_piling_up(settings):
    sql = FakeSql()
    cp = ControlPlane(sql, settings)
    cp.alert("hr", "provision_failed", "high", "Setup stopped: X", replace=True)
    closed, inserted = sql.statements
    assert closed[0].startswith("UPDATE") and "kind = :k AND bot_id <=> :b" in closed[0]
    assert closed[1] == {"k": "provision_failed", "b": "hr"} and inserted[0].startswith("INSERT INTO")
    cp.resolve_alerts(None, "availability")                                    # the platform's own
    assert sql.statements[-1][1] == {"k": "availability", "b": None}


class _Cp:
    def __init__(self):
        self.alerts, self.resolved = [], []

    def alert(self, bot_id, kind, severity, message, dedupe_key=None, required=True, replace=False):
        self.alerts.append((bot_id, kind, message, replace))

    def resolve_alerts(self, bot_id, kind):
        self.resolved.append((bot_id, kind))

    def audit(self, *a, **k):
        pass


def test_a_failed_email_alert_setup_is_recorded_as_failed_and_tried_again(settings, cfg, monkeypatch):
    from factory import alerting
    from factory.provisioning import Provisioner

    def broken(w, s, c, warehouse):
        raise TypeError("AlertV2.__init__() got an unexpected keyword argument 'custom_subject'\nTraceback ...")

    monkeypatch.setattr(alerting, "ensure_bot_alerts", broken)
    sql, cp = FakeSql(), _Cp()
    p = Provisioner(sql, settings, cp, workspace_client=object())
    assert p.create_alerts(cfg) is False
    bot_id, kind, message, replace = cp.alerts[0]
    assert kind == "alert_setup_failed" and replace and "\n" not in message and "Traceback" not in message
    assert p.retry_alerts(cfg) is False
    assert sql.statements[-1][1]["s"] == "create_alerts" and sql.statements[-1][1]["st"] == "failed"   # not "done"

    monkeypatch.setattr(alerting, "ensure_bot_alerts", lambda w, s, c, warehouse: ["a"])
    assert p.retry_alerts(cfg) is True
    assert sql.statements[-1][1]["st"] == "done" and cp.resolved[-1] == (cfg.bot_id, "alert_setup_failed")
