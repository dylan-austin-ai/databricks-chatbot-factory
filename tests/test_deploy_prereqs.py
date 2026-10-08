"""Deploy prerequisites: ordering of the shared index build, agent grants, preflight checks."""
from types import SimpleNamespace

from factory.preflight import deploy_problems
from factory.provisioning import ensure_shared_index, grant_agent_access


class FakeIndex:
    def __init__(self, log):
        self.log = log

    def wait_until_ready(self, timeout=None):
        self.log.append("wait_index")


class FakeVsc:
    def __init__(self, endpoints=(), indexes=()):
        self.log, self.endpoints, self.indexes = [], list(endpoints), list(indexes)

    def list_endpoints(self):
        return {"endpoints": [{"name": n} for n in self.endpoints]}

    def create_endpoint(self, name, endpoint_type):
        self.log.append("create_endpoint")

    def wait_for_endpoint(self, name, timeout=None):
        self.log.append("wait_endpoint")

    def list_indexes(self, name):
        return {"vector_indexes": [{"name": n} for n in self.indexes]}

    def create_delta_sync_index(self, **kwargs):
        self.log.append("create_index")

    def get_index(self, endpoint, name):
        return FakeIndex(self.log)


def test_shared_index_waits_for_endpoint_then_index(settings):
    vsc = FakeVsc()
    name = ensure_shared_index(vsc, settings)
    assert vsc.log == ["create_endpoint", "wait_endpoint", "create_index", "wait_index"]
    assert name == "chatbots_test._platform.shared_chunks_index"


def test_shared_index_still_waits_when_everything_exists(settings):
    vsc = FakeVsc(endpoints=[settings.get("ai_search.endpoint")],
                  indexes=["chatbots_test._platform.shared_chunks_index"])
    ensure_shared_index(vsc, settings)
    assert vsc.log == ["wait_endpoint", "wait_index"]


def test_grant_agent_access(settings, fake_sql):
    grant_agent_access(fake_sql, settings, "agent-sp-id")
    assert len(fake_sql.statements) == 5
    assert all("TO `agent-sp-id`" in s for s, _ in fake_sql.statements)
    assert fake_sql.find(r"READ VOLUME ON VOLUME `chatbots_test`\.`_platform`\.`runtime`")
    assert fake_sql.find(r"WRITE VOLUME ON VOLUME `chatbots_test`\.`_platform`\.`logs`")


def test_grant_agent_access_without_principal_does_nothing(settings, fake_sql):
    grant_agent_access(fake_sql, settings, "")
    assert fake_sql.statements == []


def _workspace(missing=(), secrets=None):
    secrets = secrets if secrets is not None else {
        "chatbot-factory": ["agent-sp-client-id", "agent-sp-client-secret"],
        "chatbot-factory-identity": ["pseudonym-hmac-key", "identity-fernet-key"],
    }

    def getter(kind):
        def get(name):
            if name in missing:
                raise LookupError(f"{kind} {name} does not exist")
            return name
        return SimpleNamespace(get=get)

    def list_secrets(scope):
        if scope not in secrets:
            raise LookupError(f"scope {scope} does not exist")
        return [SimpleNamespace(key=k) for k in secrets[scope]]

    return SimpleNamespace(catalogs=getter("catalog"), warehouses=getter("warehouse"),
                           serving_endpoints=getter("endpoint"),
                           secrets=SimpleNamespace(list_secrets=list_secrets))


def test_preflight_passes_when_everything_exists(settings):
    assert deploy_problems(_workspace(), settings, "wh1", "chatbot-factory-identity", "chatbot-factory") == []


def test_preflight_reports_every_missing_piece_together(settings):
    w = _workspace(missing={"chatbots_test", "wh1"},
                   secrets={"chatbot-factory": ["agent-sp-client-id"]})
    problems = deploy_problems(w, settings, "wh1", "chatbot-factory-identity", "chatbot-factory")
    assert len(problems) == 4
    assert any("Catalog 'chatbots_test'" in p and "(A5)" in p for p in problems)
    assert any("SQL warehouse 'wh1'" in p for p in problems)
    assert any("'chatbot-factory'" in p and "agent-sp-client-secret" in p for p in problems)
    assert any("'chatbot-factory-identity'" in p and "(A7)" in p for p in problems)


class _GatewayApi:
    """Model-service REST stand-in: GET raises `missing` until the service is created."""

    def __init__(self, missing=None, existing=None):
        self.calls, self.missing, self.existing = [], missing, existing or {}

    def do(self, method, path, **kwargs):
        self.calls.append(method)
        if method == "GET" and self.missing is not None:
            raise self.missing
        return self.existing


def test_model_service_is_created_on_sdk_not_found(settings):
    from databricks.sdk.errors import NotFound, ResourceDoesNotExist

    from factory.gateway import ensure_model_service

    for error in (NotFound("Resource not found"), ResourceDoesNotExist("gone")):
        api = _GatewayApi(missing=error)
        name = ensure_model_service(SimpleNamespace(api_client=api), settings, "haiku")
        assert api.calls == ["GET", "POST"]
        assert name == "chatbots_test._platform.answer_haiku"


def test_existing_model_service_is_left_untouched(settings, capsys):
    from factory.gateway import ensure_model_service, service_config

    api = _GatewayApi(existing=service_config(settings, "haiku"))
    assert ensure_model_service(SimpleNamespace(api_client=api), settings, "haiku") \
        == "chatbots_test._platform.answer_haiku"
    assert api.calls == ["GET"]
    assert capsys.readouterr().out == ""


def test_existing_model_service_with_different_routing_warns(settings, capsys):
    from factory.gateway import ensure_model_service, service_config

    other = service_config(settings, "haiku")
    other["config"]["routing"]["destinations"][0]["pay_per_token_config"]["model"] = "models/system.ai.other"
    api = _GatewayApi(existing={"name": "x", **other})
    ensure_model_service(SimpleNamespace(api_client=api), settings, "haiku")
    assert api.calls == ["GET"]
    out = capsys.readouterr().out
    assert "WARNING" in out and "models/system.ai.other" in out


def test_existing_model_service_with_unknown_shape_does_not_warn(settings, capsys):
    from factory.gateway import ensure_model_service

    api = _GatewayApi(existing={"name": "x"})
    ensure_model_service(SimpleNamespace(api_client=api), settings, "haiku")
    assert api.calls == ["GET"] and capsys.readouterr().out == ""


def test_model_service_other_errors_are_not_swallowed(settings):
    import pytest
    from databricks.sdk.errors import PermissionDenied

    from factory.gateway import ensure_model_service

    api = _GatewayApi(missing=PermissionDenied("no access"))
    with pytest.raises(PermissionDenied):
        ensure_model_service(SimpleNamespace(api_client=api), settings, "haiku")
    assert api.calls == ["GET"]


# --- release gate ---------------------------------------------------------------------------

def test_gate_passes_when_required_bots_passed():
    from factory.preflight import gate_problems

    bots = [{"bot_id": "claims", "required": True, "passed": True},
            {"bot_id": "draft", "required": False, "passed": False}]
    assert gate_problems(bots, "1.0.0", "abc123") == []


def test_gate_is_not_vacuous_on_an_empty_or_untested_qa():
    from factory.preflight import gate_problems

    assert any("no chatbot in QA has passed" in p for p in gate_problems([], "1.0.0", "abc123"))
    untested = [{"bot_id": "draft", "required": False, "passed": False}]
    assert len(gate_problems(untested, "1.0.0", "abc123")) == 1


def test_gate_names_every_required_bot_that_has_not_passed():
    from factory.preflight import gate_problems

    bots = [{"bot_id": "claims", "required": True, "passed": False},
            {"bot_id": "hr", "required": True, "passed": True}]
    problems = gate_problems(bots, "1.0.0", "abc123")
    assert len(problems) == 1 and "'claims'" in problems[0]


def test_gate_needs_a_git_commit():
    from factory.preflight import gate_problems

    assert "git commit is unknown" in gate_problems([{"bot_id": "a", "required": True, "passed": True}], "1.0.0", "")[0]


# --- model service permissions and logging ----------------------------------------------------

def test_execute_is_granted_through_the_permissions_api(settings):
    from factory.gateway import grant_execute

    calls = []
    api = SimpleNamespace(do=lambda method, path, **kw: calls.append((method, path, kw)))
    name = grant_execute(SimpleNamespace(api_client=api), settings, "haiku", ["agent-sp", "mlops"])
    assert name == "chatbots_test._platform.answer_haiku"
    assert calls == [("PATCH", "/api/2.1/unity-catalog/permissions/model_service/chatbots_test._platform.answer_haiku",
                      {"body": {"changes": [{"principal": "agent-sp", "add": ["EXECUTE"]},
                                            {"principal": "mlops", "add": ["EXECUTE"]}]}})]


def test_inference_logging_must_be_in_this_environments_catalog():
    from factory.config import PlatformSettings
    from factory.gateway import logging_problems

    def load(answer, evaluator):
        return PlatformSettings.load({"catalog": "qa_chatbot_factory", "unity_gateway.inference_table": answer,
                                      "unity_gateway.evaluator_inference_table": evaluator})

    good = load("qa_chatbot_factory._platform.gw_answer_haiku_payload",
                "qa_chatbot_factory._platform.gw_guardrail_evaluator_payload")
    assert logging_problems(good, lambda t: True) == []
    assert len(logging_problems(good, lambda t: False)) == 2            # recorded but not created
    assert len(logging_problems(load("", ""), lambda t: True)) == 2      # not recorded
    wrong = load("prod_chatbot_factory._platform.gw_answer_haiku_payload",
                 "qa_chatbot_factory._platform.gw_guardrail_evaluator_payload")
    problems = logging_problems(wrong, lambda t: True)
    assert len(problems) == 1 and "outside this environment" in problems[0]


# --- endpoint housekeeping --------------------------------------------------------------------

class _Endpoints:
    def __init__(self, served):
        self.served, self.waits = served, 0

    def wait_get_serving_endpoint_not_updating(self, name, timeout=None):
        self.waits += 1
        entities = [SimpleNamespace(entity_name="c.s.agent", entity_version=v) for v in self.served]
        return SimpleNamespace(config=SimpleNamespace(served_entities=entities))


def test_wait_until_serving_requires_the_new_version():
    import pytest

    from factory.serving import wait_until_serving

    wait_until_serving(SimpleNamespace(serving_endpoints=_Endpoints(["4", "5"])), "ep", "c.s.agent", 5)
    with pytest.raises(RuntimeError, match="not serving"):
        wait_until_serving(SimpleNamespace(serving_endpoints=_Endpoints(["4"])), "ep", "c.s.agent", 5)


def test_old_versions_are_removed_one_update_at_a_time():
    from factory.serving import remove_old_versions

    deleted = []
    agents = SimpleNamespace(
        get_deployments=lambda model: [SimpleNamespace(model_version=v) for v in ("3", "4", "5")],
        delete_deployment=lambda model, model_version: deleted.append((model, model_version)))
    endpoints = _Endpoints(["5"])
    removed = remove_old_versions(SimpleNamespace(serving_endpoints=endpoints), agents, "ep", "c.s.agent", 5)
    assert removed == ["3", "4"]
    assert deleted == [("c.s.agent", 3), ("c.s.agent", 4)]   # always with an explicit version
    assert endpoints.waits == 2


def test_ai_search_endpoint_gets_the_usage_policy(settings):
    vsc = FakeVsc()
    created = {}
    vsc.create_endpoint = lambda **kw: created.update(kw)
    ensure_shared_index(vsc, settings, usage_policy_id="pol-qa")
    assert created == {"name": settings.get("ai_search.endpoint"), "endpoint_type": "STANDARD",
                       "usage_policy_id": "pol-qa"}
