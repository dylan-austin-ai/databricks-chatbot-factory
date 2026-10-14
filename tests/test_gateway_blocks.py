"""Which guardrail blocked an answer, whether the settings say it may, and how a check reports it."""
import pathlib

from factory import evals, gateway

ROOT = pathlib.Path(__file__).resolve().parents[1]
# The block the QA diagnostic hit, as the gateway described it.
HALLUCINATION = {"databricks_service_policy": {
    "result": "DENY", "policy": "hallucination", "handler": "system.ai.block_hallucination", "stage": "post_call",
    "dry_run": False, "evaluator": "system.ai.gpt-5-2",
    "reason": "The response makes specific factual claims ... not supported by the provided excerpt."}}


def test_a_block_says_which_guardrail_and_where():
    assert gateway.block_details(HALLUCINATION) == {
        "reason": "The response makes specific factual claims ... not supported by the provided excerpt.",
        "policy": "hallucination", "action": "deny", "handler": "system.ai.block_hallucination", "stage": "post_call",
        "dry_run": False}
    # The QA unsafe-content blocks: the policy's name is kept even when no guardrail name comes with it.
    unsafe = {"databricks_service_policy": {"policy": "unsafe_content", "action": "deny", "stage": "post_call",
                                            "reason": "Contains contact information considered PII."}}
    said = gateway.block_details(unsafe)
    assert (said["policy"], said["action"], said["stage"], said["handler"]) == ("unsafe_content", "deny", "post_call", "")
    assert evals.block_sentence(said) == "BLOCKED by unsafe_content at the post_call stage."
    assert gateway.blocked_reason(HALLUCINATION).startswith("The response makes")    # unchanged for its callers


def test_the_guardrail_is_found_under_another_field_name_and_missing_fields_stay_empty():
    other = {"databricks_service_policy": {"decision": "BLOCK", "name": "system.ai.block_jailbreak", "reason": "x"}}
    assert gateway.block_details(other) == {"reason": "x", "policy": "system.ai.block_jailbreak", "action": "block",
                                            "handler": "system.ai.block_jailbreak", "stage": "", "dry_run": None}
    assert gateway.block_details({"databricks_service_policy": "denied"})["reason"] == "denied"
    assert gateway.block_details({"databricks_service_policy": {"result": "ALLOW"}}) is None
    assert gateway.block_details({"choices": []}) is None


def test_a_block_from_a_policy_the_settings_put_in_log_mode_is_flagged(settings):
    mode = {p["policy"]: p["mode"] for p in settings.get("unity_gateway.policies")}
    assert mode["system.ai.block_hallucination"] == "log" and mode["system.ai.block_jailbreak"] == "enforce"
    drift = gateway.blocked_against_settings("system.ai.block_hallucination", settings)
    assert "should only log (Log mode)" in drift and "set to Enforce" in drift and "SETUP C2" in drift
    assert gateway.blocked_against_settings("system.ai.block_jailbreak", settings) == ""   # meant to block
    assert gateway.blocked_against_settings("", settings) == ""                           # unknown: no claim


def test_the_diagnostic_counts_every_outcome_instead_of_calling_blocks_answers():
    assert evals.outcome_counts(["answered", "blocked", "blocked"]) == "answered 1, blocked by a guardrail 2"
    said = evals.outcome_counts(["refused", "answered", "", "error", "no_source"])
    assert said == ("answered 1, declined as out of scope 1, said its documents don't cover it 1, "
                    "failed with an error 1, not asked 1")
    assert evals.outcome_counts(["something_new"]) == "something_new 1"
    job = (ROOT / "jobs" / "run_evals.py").read_text()
    assert "the chatbot gave \"\n                  f\"{faults} of them no answer" not in job
    assert "evals.outcome_counts([outcome_of(c) for c in cases])" in job


def test_a_blocked_question_names_the_guardrail_and_the_mismatch():
    note = {"policy": "hallucination", "handler": "system.ai.block_hallucination", "stage": "post_call",
            "settings_mismatch": "system.ai.block_hallucination blocked this, but the settings say it should only log"}
    line = evals.block_sentence(note)
    assert line.startswith("BLOCKED by hallucination (system.ai.block_hallucination) at the post_call stage.")
    assert "SETTINGS MISMATCH: system.ai.block_hallucination blocked this" in line
    assert evals.block_sentence({}) == "BLOCKED by a Unity Gateway service policy (the gateway didn't say which)."


def test_only_the_platforms_own_callers_are_told_which_guardrail_blocked_them():
    agent = (ROOT / "agent" / "agent.py").read_text()
    assert ('"blocked_by": (self._block_note(log) if log["outcome"] == "blocked" and log.get("synthetic")\n'
            '                               else None),') in agent
    assert "raise _GatewayBlocked(block)" in agent and "gw.blocked_reason(" not in agent
    assert 'drift = gw.blocked_against_settings(block["handler"], s)' in agent
    assert '_event(log, "gateway_policy_mode", "flag", drift)' in agent
    assert '"policy": block.get("policy"),' in agent and '"action": block.get("action"),' in agent
