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
    body = source[source.index("def agent_outputs("):source.index("def chatbot_error(")]
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


def test_the_deploy_and_the_observability_task_grant_the_serving_identity():
    for path in ("agent/deploy.py", "jobs/setup_observability.py"):
        source = (ROOT / path).read_text()
        assert "trace_grants(" in source and 's.get("access.serving_identity")' in source, path


def test_a_chatbot_error_fails_the_check_as_a_fault_not_as_a_bad_answer(settings):
    thresholds = settings.thresholds()
    good = {name: 1.0 for name in thresholds}
    passed = evals.gate(good, thresholds, {}, [], 60, 50)
    assert passed.passed
    failed = evals.gate(good, thresholds, {}, [], 60, 50, agent_errors=3)
    assert not failed.passed and failed.failures == [
        "The chatbot answered 3 question(s) with an error instead of an answer; this must be zero."]
    advice = problems.check_advice(failed.failures[0])
    assert "fault in the chatbot itself" in advice and "MLOps" in advice and "Diagnose the quality check" in advice

    source = (ROOT / "jobs" / "run_evals.py").read_text()
    assert "nothing to score for" in source and source.index("nothing to score for") < source.index("mlflow.genai.evaluate(data=records")
    assert 'agent_errors=sum(1 for c in cases if OUTCOMES.get(c["question"]) == "error")' in source


def test_the_agent_gives_its_reason_only_to_the_platforms_own_test_callers():
    source = (ROOT / "agent" / "agent.py").read_text()
    assert '"error": log["error"] if log["outcome"] == "error" and self._may_see_error(ci, s, log) else None' in source
    rule = source[source.index("def _may_see_error("):source.index("def _save_turn(")]
    assert 'if not ci.get("synthetic"):\n            return False' in rule          # a person asking never is
    assert 'access.jobs_run_as' in rule and "access.synthetic_callers" in rule
    assert "except Exception:  # noqa: BLE001 - when in doubt, no\n            return False" in rule
    log = {"request_id": "r", "tokens": [0, 0], "outcome": "error", "error_type": "PermissionDenied", "bot_id": "b"}
    assert trace_tags(log)["error_type"] == "PermissionDenied"                    # the kind, never the message
    assert "error" not in {k for k in trace_tags(log) if k != "error_type"}
