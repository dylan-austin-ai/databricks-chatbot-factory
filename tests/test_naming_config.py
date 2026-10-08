import pytest

from factory.config import BotConfig, PlatformSettings
from factory.naming import NamingError, normalize, validate


@pytest.mark.parametrize("name,expected", [
    ("Claims Chatbot", "claims_chatbot"),
    ("  HR -- Policy Bot!! ", "hr_policy_bot"),
    ("Café Menü", "cafe_menu"),
    ("2024 Benefits", "bot_2024_benefits"),
])
def test_normalize(name, expected):
    assert normalize(name) == expected


def test_collision_is_case_and_punctuation_insensitive():
    with pytest.raises(NamingError, match="already exists"):
        validate("claims-chatbot", {"claims_chatbot"})


@pytest.mark.parametrize("name", ["", "   ", "!!!", "Default", "x" * 80])
def test_invalid_names(name):
    with pytest.raises(NamingError):
        validate(name, set())


def test_overrides_merge_without_losing_defaults():
    s = PlatformSettings.load({"quality.judge_sample_rate": 0.5, "catalog": "c1"})
    assert s.get("quality.judge_sample_rate") == 0.5
    assert s.get("quality.go_live_thresholds.correctness") == 0.90
    assert s.catalog == "c1"
    assert s.fq("bots") == "c1._platform.bots"


def test_only_guardrailed_models_allowed(settings):
    assert settings.answer_service("haiku") == "chatbots_test._platform.answer_haiku"
    with pytest.raises(ValueError, match="model service"):
        settings.answer_service("some-unguarded-model")


def test_bot_config_rules(cfg):
    assert cfg.validate() == []
    cfg.access_mode, cfg.sensitivity = "middleware", "internal"
    assert any("Public" in p for p in cfg.validate())
    cfg.sensitivity = "public"
    assert cfg.validate() == []
    cfg.identity_mode, cfg.history_mode = "pseudonymous", "saved"
    assert any("pseudonymous" in p for p in cfg.validate())
    cfg.history_mode, cfg.testers = "session", [f"t{i}@corp.com" for i in range(11)]
    assert any("10 testers" in p for p in cfg.validate())


def test_confidential_gets_dedicated_index(cfg):
    assert not cfg.dedicated_index
    cfg.sensitivity = "confidential"
    assert cfg.dedicated_index


def test_config_roundtrip(cfg):
    import json
    assert BotConfig.from_dict(json.loads(cfg.to_json())) == cfg


def test_environment_comes_from_the_deployment(monkeypatch):
    from factory.config import PlatformSettings

    monkeypatch.delenv("FACTORY_ENV", raising=False)
    assert PlatformSettings.load().get("environment") == "prod"   # packaged default
    monkeypatch.setenv("FACTORY_ENV", "qa")
    assert PlatformSettings.load().get("environment") == "qa"
    assert PlatformSettings.load({"environment": "prod"}).get("environment") == "qa"   # saved settings can't override it


def test_gateway_calls_are_tagged_with_the_environment():
    import json

    from factory.gateway import request_tags

    header = request_tags({"bot_id": "b", "channel": "candidate", "environment": "qa", "request_id": "r1"})
    tags = json.loads(header["Databricks-Ai-Gateway-Request-Tags"])
    assert tags["environment"] == "qa" and tags["channel"] == "candidate"


def test_deployment_scope_rejects_unsafe_values(settings, fake_sql, monkeypatch):
    import pytest
    from types import SimpleNamespace

    from factory.config import PlatformSettings
    from factory.controlplane import ControlPlane

    w = SimpleNamespace(get_workspace_id=lambda: 1234567890)
    monkeypatch.setenv("FACTORY_ENV", "qa")
    assert ControlPlane(fake_sql, PlatformSettings.load(), w).deployment_scope() == {
        "environment": "qa", "workspace_id": "1234567890"}
    monkeypatch.setenv("FACTORY_ENV", "qa' OR 1=1 --")
    with pytest.raises(ValueError):
        ControlPlane(fake_sql, PlatformSettings.load(), w).deployment_scope()
