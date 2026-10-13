"""Span values read back from storage, who may write traces, and chatbot errors in a quality check."""
import pathlib

from factory import evals, problems
from factory.tracing import TRACE_TABLES, as_dict, trace_grants, trace_tags

ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_span_values_stored_as_text_are_read_as_dictionaries():
    assert as_dict({"outcome": "answered"}) == {"outcome": "answered"}
    assert as_dict('{"outcome": "answered", "retrieval": [{"chunk_id": "c1"}]}')["retrieval"][0]["chunk_id"] == "c1"
    assert as_dict(b'{"a": 1}') == {"a": 1}
    for odd in (None, "", "not json", "[1, 2]", '"just text"', 7, ["x"]):
        assert as_dict(odd) == {}, odd                    # never something .get() fails on


def test_the_check_reads_the_chatbots_output_through_that():
    source = (ROOT / "jobs" / "run_evals.py").read_text()
    body = source[source.index("def agent_outputs("):source.index("def runs_as(")]
    assert "as_dict(spans[0].outputs)" in body and "if trace is not None" in body
    assert 'as_dict(t.data.request).get("question")' in source


def test_trace_writers_get_the_catalog_and_schema_as_well_as_the_tables():
    tables = ["qa_cf._platform.traces_otel_spans", "qa_cf._platform.traces_otel_annotations"]
    grants = trace_grants("qa_cf", "_platform", ["agent-sp", "", "serving-app-id", "agent-sp", None], ["mlops", None], tables)
    assert grants[:4] == ["GRANT USE CATALOG ON CATALOG `qa_cf` TO `agent-sp`",
                          "GRANT USE CATALOG ON CATALOG `qa_cf` TO `serving-app-id`",
                          "GRANT USE SCHEMA ON SCHEMA `qa_cf`.`_platform` TO `agent-sp`",
                          "GRANT USE SCHEMA ON SCHEMA `qa_cf`.`_platform` TO `serving-app-id`"]
    assert "GRANT MODIFY, SELECT ON TABLE `qa_cf`.`_platform`.`traces_otel_annotations` TO `serving-app-id`" in grants
    assert "GRANT SELECT ON TABLE `qa_cf`.`_platform`.`traces_otel_spans` TO `mlops`" in grants
    assert not [g for g in grants if "MODIFY" in g and "mlops" in g]              # readers only read
    assert len(grants) == 4 + 2 * (2 + 1) and "annotations" in TRACE_TABLES
    assert trace_grants("qa_cf", "_platform", [], [], tables) == []


def test_the_trace_writer_is_the_dedicated_agent_principal_not_an_identity_someone_has_to_find():
    deploy = (ROOT / "agent" / "deploy.py").read_text()
    assert 'writers = [a.agent_principal, s.get("access.app_service_principal")]' in deploy
    # the saved serving identity is the agent principal that was chosen, written by the deploy itself
    assert '"serving_identity": a.agent_principal}, "system:deploy")' in deploy
    assert "Serving endpoint identity and rerun" not in deploy                    # no hunting for a system principal
    task = (ROOT / "jobs" / "setup_observability.py").read_text()
    assert 'args("catalog", "environment", "warehouse_id", "agent_principal")' in task
    assert '_writers = [a.agent_principal, s.get("access.serving_identity"), s.get("access.app_service_principal")]' in task
    import yaml

    jobs = yaml.safe_load((ROOT / "resources" / "jobs.yml").read_text())["resources"]["jobs"]["deploy_agent"]["tasks"]
    for task_def in jobs:   # both tasks are told who the agent principal is
        assert "--agent_principal=${var.agent_principal}" in task_def["spark_python_task"]["parameters"], task_def["task_key"]


def test_a_chatbot_error_fails_the_check_as_a_fault_not_as_a_bad_answer(settings):
    thresholds = settings.thresholds()
    good = {name: 1.0 for name in thresholds}
    passed = evals.gate(good, thresholds, {}, [], 60, 50)
    assert passed.passed
    failed = evals.gate(good, thresholds, {}, [], 60, 50, agent_errors=3)
    assert not failed.passed and failed.failures == [
        "The chatbot gave 3 question(s) no answer at all (an error, or access refused); this must be zero."]
    advice = problems.check_advice(failed.failures[0])
    assert "fault in the chatbot itself" in advice and "MLOps" in advice and "Diagnose the quality check" in advice

    source = (ROOT / "jobs" / "run_evals.py").read_text()
    assert "nothing to score for" in source and source.index("nothing to score for") < source.index("mlflow.genai.evaluate(data=records")
    assert 'faults = sum(1 for c in cases if OUTCOMES.get(c["question"]) in evals.CANT_BE_SCORED)' in source
    assert "agent_errors=faults" in source and evals.CANT_BE_SCORED == ("error", "denied", "unavailable")


def test_the_agent_gives_its_reason_only_to_the_platforms_own_test_callers():
    source = (ROOT / "agent" / "agent.py").read_text()
    assert '"error": log["error"] if log["outcome"] == "error" and self._may_see_error(ci, s, log) else None' in source
    rule = source[source.index("def _may_see_error("):source.index("def _save_turn(")]
    assert 'if not ci.get("synthetic"):\n            return False' in rule          # a person asking never is
    assert "return idn.is_platform_caller(caller, s)" in rule                      # the same rule as for access
    assert "except Exception:  # noqa: BLE001 - when in doubt, no\n            return False" in rule
    log = {"request_id": "r", "tokens": [0, 0], "outcome": "error", "error_type": "PermissionDenied", "bot_id": "b"}
    assert trace_tags(log)["error_type"] == "PermissionDenied"                    # the kind, never the message
    assert "error" not in {k for k in trace_tags(log) if k != "error_type"}


# --- the endpoint signs in explicitly as the agent service principal ---------------------------

def test_the_endpoint_is_given_explicit_oauth_the_workspace_host_and_secret_backed_credentials():
    import pytest

    from factory.serving import agent_identity_env

    env = agent_identity_env("https://dbc-1234.cloud.databricks.com/", "chatbot-factory")
    assert env == {"DATABRICKS_HOST": "https://dbc-1234.cloud.databricks.com",       # the deploying workspace
                   "DATABRICKS_AUTH_TYPE": "oauth-m2m",                               # stated, not left to chance
                   "DATABRICKS_CLIENT_ID": "{{secrets/chatbot-factory/agent-sp-client-id}}",
                   "DATABRICKS_CLIENT_SECRET": "{{secrets/chatbot-factory/agent-sp-client-secret}}"}
    # a different workspace gets its own host: nothing about QA is built in
    assert agent_identity_env("https://prod.cloud.databricks.com", "s")["DATABRICKS_HOST"] == "https://prod.cloud.databricks.com"
    for name in ("DATABRICKS_CLIENT_ID", "DATABRICKS_CLIENT_SECRET"):   # references to the secret scope, never values
        assert env[name].startswith("{{secrets/") and env[name].endswith("}}")
    for bad_host in ("", None, "dbc-1234.cloud.databricks.com", "http://insecure"):
        with pytest.raises(ValueError):
            agent_identity_env(bad_host, "scope")
    with pytest.raises(ValueError):
        agent_identity_env("https://x.cloud.databricks.com", "")


def test_the_deploy_takes_the_host_from_its_own_workspace():
    deploy = (ROOT / "agent" / "deploy.py").read_text()
    assert "**agent_identity_env(w.config.host, a.agent_sp_scope)," in deploy
    environment = deploy[deploy.index("environment_vars={"):deploy.index("wanted_tags = ")]
    assert "agent_identity_env(" in environment and "cloud.databricks.com" not in deploy and "azuredatabricks" not in deploy
    assert '"DATABRICKS_CLIENT_SECRET": f"' not in deploy          # built in one place, with the host and the method


def test_grants_are_reported_as_they_actually_went():
    from factory.tracing import apply_grants, grant_report

    statements = ["GRANT USE CATALOG ON CATALOG `qa` TO `agent-sp`", "GRANT USE SCHEMA ON SCHEMA `qa`.`_platform` TO `agent-sp`",
                  "GRANT MODIFY, SELECT ON TABLE `qa`.`_platform`.`traces_otel_annotations` TO `agent-sp`"]
    ran = []

    def execute(statement):
        ran.append(statement)
        if "traces_otel_annotations" in statement:
            raise PermissionError("PERMISSION_DENIED: User does not have MANAGE on Table 'qa._platform.traces_otel_annotations'. "
                                  "token=dapi0123456789abcdef0123\nJVM stacktrace: org.apache.spark...")

    done, failed = apply_grants(execute, statements)
    assert ran == statements and done == statements[:2]               # one refusal doesn't stop the others
    assert failed[0][0] == statements[2]                              # the statement that failed, whole
    reason = failed[0][1]
    assert reason.startswith("PermissionError: PERMISSION_DENIED: User does not have MANAGE on Table")
    assert "dapi0123456789abcdef0123" not in reason and "[hidden]" in reason and "JVM" not in reason
    report = grant_report(done, failed)
    assert report.splitlines()[0] == "Trace table grants: 2 succeeded, 1 FAILED."
    assert f"FAILED: {statements[2]}" in report and "because: PermissionError" in report
    assert "applied" not in report and "all" not in report.splitlines()[0]
    assert grant_report(statements, []) == "Trace table grants: all 3 succeeded."
    assert grant_report([], []) == "Trace table grants: nothing to grant."


def test_the_observability_task_never_says_applied_for_grants_that_failed():
    task = (ROOT / "jobs" / "setup_observability.py").read_text()
    assert "apply_grants(sql.execute, trace_grants(" in task and "print(grant_report(_granted, _refused))" in task
    assert "Trace table grants applied" not in task and "Trace table grant skipped" not in task
    deploy = (ROOT / "agent" / "deploy.py").read_text()
    assert "apply_grants(spark.sql, trace_grants(" in deploy and "print(grant_report(granted, refused))" in deploy
    assert deploy.index("print(grant_report(granted, refused))") < deploy.index("trace table grant(s) failed")


def test_a_printed_reason_hides_anything_that_looks_like_a_credential():
    for leak in ("failed with Bearer eyJhbGciOi.abc.def in header", "client_secret=s3cr3t-value rejected",
                 "token: dapiabcdef0123456789", "password = hunter2"):
        reason = problems.safe_reason(RuntimeError(leak))
        assert "[hidden]" in reason, leak
        for secret in ("eyJhbGciOi.abc.def", "s3cr3t-value", "dapiabcdef0123456789", "hunter2"):
            assert secret not in reason
    assert problems.safe_reason(PermissionError("User does not have MANAGE")) == "PermissionError: User does not have MANAGE"
