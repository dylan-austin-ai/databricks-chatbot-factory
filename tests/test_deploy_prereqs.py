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
