"""Wizard restore point and the admin archive/delete paths."""
import pytest

from factory.config import EVERYONE, BotConfig
from factory.lifecycle import State, TransitionError, check_transition, retire_path
from factory.wizard import UNFINISHED_STATES, answers_from_config


def test_saved_config_refills_the_wizard(cfg):
    cfg.allowed_principals = ["claims-adjusters", "jane@corp.com"]
    cfg.channels = ["chat", "api"]
    cfg.testers = ["sam@corp.com"]
    cfg.alert_prefs = {"traffic": False}
    a = answers_from_config(cfg)
    assert (a["resume"], a["bot_id"], a["mode"], a["step"]) == ("claims_chatbot", "claims_chatbot", "advanced", 5)
    assert a["display_name"] == "Claims Chatbot" and a["purpose"].startswith("Answers adjusters")
    assert a["allowed_groups"] == ["claims-adjusters"] and a["allowed_people"] == ["jane@corp.com"]
    assert a["channel"] == "both" and a["traffic_alerts"] is False
    assert a["reviewer"] == "lead@corp.com" and a["reviewer_group"] == ""
    assert a["refuse_topics"] == ["legal advice"] and a["testers"] == ["sam@corp.com"]


def test_restored_answers_rebuild_the_same_config(cfg):
    """What the wizard page builds from the restored answers matches what was saved."""
    cfg.allowed_principals = [EVERYONE]
    cfg.reviewer = "claims-leads"
    a = answers_from_config(cfg)
    rebuilt = BotConfig(
        bot_id=a["bot_id"], display_name=a["display_name"], purpose=a["purpose"], owner_user=a["owner_user"],
        owner_group=a["owner_group"], business_function=a["business_function"], access_mode=a["access_mode"],
        allowed_principals=a["allowed_principals"],
        channels=["chat", "api"] if a["channel"] == "both" else [a["channel"]],
        refuse_topics=a["refuse_topics"], answer_style=a["answer_style"], history_mode=a["history_mode"],
        identity_mode=a["identity_mode"], sensitivity=a["sensitivity"], source_type=a["source_type"],
        source_uri=a["source_uri"], golden_set_mode=a["golden_set_mode"], reviewer=a["reviewer"],
        testers=a["testers"], limit_quotes=a["limit_quotes"], alert_prefs={"traffic": a["traffic_alerts"]})
    for name in ("bot_id", "display_name", "purpose", "owner_user", "owner_group", "allowed_principals", "channels",
                 "refuse_topics", "reviewer", "sensitivity", "history_mode", "identity_mode", "limit_quotes"):
        assert getattr(rebuilt, name) == getattr(cfg, name), name
    assert a["allowed_groups"] == [] and a["reviewer_group"] == "claims-leads"


def test_unfinished_states_are_the_ones_setup_can_resume_from():
    assert set(UNFINISHED_STATES) == {"draft", "provision_failed"}
    for state in UNFINISHED_STATES:   # the provision job moves both to "provisioning"
        check_transition(state, State.PROVISIONING.value)


@pytest.mark.parametrize("state,expected", [
    ("live", ["archived"]), ("testing", ["archived"]), ("paused", ["archived"]),
    ("budget_paused", ["archived"]), ("pending_approval", ["archived"]), ("archived", []),
])
def test_admin_can_archive_any_chatbot_that_was_set_up(state, expected):
    assert retire_path(state, "archived") == expected


@pytest.mark.parametrize("state", ["draft", "provisioning", "provision_failed"])
def test_a_chatbot_that_was_never_set_up_cannot_be_archived(state):
    with pytest.raises(TransitionError):
        retire_path(state, "archived")


@pytest.mark.parametrize("state,expected", [
    ("draft", ["deleted"]), ("provisioning", ["deleted"]), ("provision_failed", ["deleted"]),
    ("archived", ["deleted"]), ("deleted", []),
    ("live", ["archived", "deleted"]), ("testing", ["archived", "deleted"]),
    ("pending_approval", ["archived", "deleted"]), ("paused", ["archived", "deleted"]),
])
def test_admin_can_delete_from_every_state(state, expected):
    path = retire_path(state, "deleted")
    assert path == expected
    current = state
    for step in path:   # every hop is a legal state change
        check_transition(current, step)
        current = step


def test_retire_path_only_retires():
    with pytest.raises(ValueError):
        retire_path("archived", "live")
    with pytest.raises(TransitionError):
        retire_path("purged", "deleted")
