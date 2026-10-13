"""The agent works out who is asking from the caller's own credentials, or answers nobody."""
import pathlib
from types import SimpleNamespace

import pytest

from factory import evals, problems
from factory.identity import NO_CALLER, CallerUnknown, obo_caller

ROOT = pathlib.Path(__file__).resolve().parents[1]
AGENT_SP = "1f0e2d3c-aaaa-bbbb-cccc-0123456789ab"


def client_for(user_name=None, emails=(), display_name=None, fails=None):
    def me():
        if fails:
            raise fails
        return SimpleNamespace(user_name=user_name, display_name=display_name,
                               emails=[SimpleNamespace(value=e) for e in emails])
    return SimpleNamespace(current_user=SimpleNamespace(me=me))


def test_the_caller_is_whoever_the_user_token_says_and_the_client_acts_as_them():
    client = client_for("ana@corp.com")
    assert obo_caller(lambda: client, AGENT_SP) == ("ana@corp.com", client)
    assert obo_caller(lambda: client_for(None, ["ana@corp.com"]), AGENT_SP)[0] == "ana@corp.com"
    assert obo_caller(lambda: client_for("jobs-sp-app-id"), AGENT_SP)[0] == "jobs-sp-app-id"   # a service principal
    assert obo_caller(lambda: client)[0] == "ana@corp.com"                 # local testing: no own identity to compare


def test_no_user_token_is_an_explicit_refusal_not_a_switch_to_the_agents_identity():
    built = []

    def no_token():
        built.append("user client")
        raise ValueError("model_serving_user_credentials auth: Unable to detect credentials for user authorization.\n"
                         "token=abc123 more detail")

    with pytest.raises(CallerUnknown) as stopped:
        obo_caller(no_token, AGENT_SP)
    assert built == ["user client"]                                        # tried once; nothing else was tried
    assert isinstance(stopped.value, PermissionError)
    assert str(stopped.value) == (f"{NO_CALLER}: ValueError: model_serving_user_credentials auth: Unable to detect "
                                  "credentials for user authorization.")  # the detail, for the restricted request log
    assert stopped.value.public == f"{NO_CALLER} (ValueError)"             # anyone: the kind, never the message
    assert "token" not in stopped.value.public and "Unable to detect" not in stopped.value.public


def test_a_token_that_cannot_say_who_it_is_is_refused_too():
    denied = RuntimeError("PermissionDenied: Invalid scope for /scim/v2/Me")
    with pytest.raises(CallerUnknown) as stopped:
        obo_caller(lambda: client_for(fails=denied), AGENT_SP)
    assert stopped.value.public == f"{NO_CALLER} (RuntimeError)" and stopped.value.__cause__ is denied
    with pytest.raises(CallerUnknown) as nameless:
        obo_caller(lambda: client_for(None, [], "  "), AGENT_SP)
    assert "no user name" in str(nameless.value)


def test_the_agents_own_identity_is_never_accepted_as_the_person_asking():
    for same in (AGENT_SP, AGENT_SP.upper(), f" {AGENT_SP} "):
        with pytest.raises(CallerUnknown) as stopped:
            obo_caller(lambda: client_for(AGENT_SP), same)
        assert "the agent's own" in str(stopped.value) and stopped.value.public == f"{NO_CALLER} (agent's own identity)"


def test_the_agent_has_no_fallback_identity_and_says_so_plainly():
    source = (ROOT / "agent" / "agent.py").read_text()
    caller = source[source.index("    def caller(self):"):source.index("    def _probe(")]
    assert 'return idn.obo_caller(as_caller, os.environ.get("DATABRICKS_CLIENT_ID", ""))' in caller
    assert "WorkspaceClient(credentials_strategy=ModelServingUserCredentials())" in caller
    assert "WorkspaceClient()" not in caller and "except" not in caller    # the old silent switch
    # built for each request, inside predict: Model Serving only has the user's token then
    assert source.index("def predict(self") < source.index("caller, obo = self.caller()")
    assert "self.caller()" not in source[source.index("def __init__(self):"):source.index("def caller(self):")]
    handled = source[source.index("result = self._answer(msgs, ci, s, log)"):source.index('log["answer"] = result["text"]')]
    assert handled.index("except idn.CallerUnknown as e:") < handled.index("except Exception as e:")
    assert 'result, shared = {**result, "text": MSG["no_caller"]}, e.public' in handled
    assert '"error", str(e)[:1000], type(e).__name__' in handled             # outcome error, error_type CallerUnknown
    assert "asking again won't help" in source and "not something you did" in source


def test_the_deploy_declares_the_user_authorisation_the_agent_depends_on():
    deploy = (ROOT / "agent" / "deploy.py").read_text()
    assert "user_auth_policy=UserAuthPolicy(api_scopes=" in deploy and "auth_policy=auth," in deploy
    assert '"databricks-ai-bridge>=0.6"' in deploy


def test_the_quality_check_names_the_cause_and_what_to_check_and_leaves_the_jobs_alone():
    reason = CallerUnknown("ValueError: no token", "ValueError").public
    fault = evals.chatbot_fault({"outcome": "error", "error": reason}, "admin@corp.com", recognised=True)
    assert NO_CALLER in fault and "(ValueError)" in fault
    assert "Nothing about the jobs or their identity needs changing" in fault
    assert "'Agent Framework: On-Behalf-Of-User Authorization'" in fault and "deploy_agent" in fault
    assert "Databricks support" in fault and "ValueError means no token arrived" in fault
    assert fault.index("(1)") < fault.index("(2)") < fault.index("(3)")
    other = evals.chatbot_fault({"outcome": "error", "error": "PermissionDenied: no"})
    assert "On-Behalf-Of" not in other                                      # only for this cause
    job = (ROOT / "jobs" / "run_evals.py").read_text()
    assert "(request {said.get('request_id') or 'not given'})" in job       # what a support case needs
    stopped = f"The chatbot gave a test question no answer, so there is nothing to score for hr_chatbot: {fault}"
    assert problems.job_stopped(stopped)[0] == problems.SETUP               # starting it again won't help
