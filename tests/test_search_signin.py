"""The agent's AI Search client signs in as the agent service principal, explicitly."""
import pathlib

import pytest

from factory.serving import agent_identity_env, agent_search_credentials

ROOT = pathlib.Path(__file__).resolve().parents[1]


def endpoint_env(host="https://qa.cloud.databricks.com/", scope="chatbot-factory"):
    """What Model Serving gives the agent: the deploy's variables, with the secrets filled in."""
    env = agent_identity_env(host, scope)
    secrets = {f"{{{{secrets/{scope}/agent-sp-client-id}}}}": "agent-client-id",
               f"{{{{secrets/{scope}/agent-sp-client-secret}}}}": "agent-client-secret"}
    return {k: secrets.get(v, v) for k, v in env.items()}


def test_the_search_client_gets_the_agent_principals_own_credentials():
    assert agent_search_credentials(endpoint_env()) == {
        "workspace_url": "https://qa.cloud.databricks.com",             # this workspace, as the deploy set it
        "service_principal_client_id": "agent-client-id",
        "service_principal_client_secret": "agent-client-secret"}


def test_it_reads_exactly_what_the_deploy_puts_on_the_endpoint():
    deployed = set(agent_identity_env("https://qa.cloud.databricks.com", "s"))
    for name in ("DATABRICKS_HOST", "DATABRICKS_CLIENT_ID", "DATABRICKS_CLIENT_SECRET"):
        assert name in deployed, name
        without = {k: v for k, v in endpoint_env().items() if k != name}
        with pytest.raises(RuntimeError) as stopped:
            agent_search_credentials(without)
        assert name in str(stopped.value) and "deploy_agent" in str(stopped.value)
        assert "agent-client-secret" not in str(stopped.value)           # never the secret itself


def test_the_agent_builds_its_search_client_with_those_arguments_and_not_as_the_caller():
    source = (ROOT / "agent" / "agent.py").read_text()
    vsc = source[source.index("    def vsc(self):"):source.index("    def _read_json(")]
    assert "VectorSearchClient(disable_notice=True, **agent_search_credentials())" in vsc
    assert "VectorSearchClient(disable_notice=True)" not in source         # the client that got no credentials
    assert "credential_strategy" not in vsc and "ModelServingUserCredentials" not in vsc
    caller = source[source.index("    def caller(self):"):source.index("    def _probe(")]
    assert "ModelServingUserCredentials" in caller                       # the caller is still worked out from OBO


def test_the_deploy_grants_the_indexes_before_the_new_agent_serves():
    deploy = (ROOT / "agent" / "deploy.py").read_text()
    grants = deploy.index("search_grants([index_name, *ours], a.agent_principal)")
    assert deploy.index("index_name = ensure_shared_index(") < grants < deploy.index('step(9, "Logging, registering')
    assert 'print(grant_report(granted, refused, "Search index grants"))' in deploy
    assert 'n.startswith(f"{a.catalog}.") and n.endswith(".chunks_index")' in deploy   # this environment's own only
    assert '"vector-search-endpoints"' in deploy and "PermissionLevel.CAN_USE" in deploy
