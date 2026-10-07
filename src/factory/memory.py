"""Server-side conversation history on Databricks managed sessions (Beta, Lakebase-backed; CNV-2).

Lets API callers send only the new question plus a conversation_id: the agent loads earlier
turns from the session and appends the new turn. Access control is at the store level (only the
agent's service principal can read it); actor_id partitions by user. The managed-sessions SDK is
Beta and its session-store calls are not fully documented, so every call is best effort: if the
package or call is unavailable, the agent behaves exactly as before (history from the caller).
VERIFY the AgentKitClient session API names in a dev workspace.
"""
from __future__ import annotations


class ManagedSessions:
    def __init__(self, workspace_client, store_name: str):
        from databricks_agentkit import AgentKitClient
        self.client = AgentKitClient(workspace_client)
        self.store = self.client.session_stores.get_or_create(store_name)

    def history(self, session_id: str, actor_id: str, limit: int) -> list[dict]:
        session = self.store.get_or_create_session(session_id, actor_id=actor_id)
        items = session.list_items(order_by="create_time asc")
        return [{"role": i["role"], "content": i["content"]} for i in list(items)[-limit:]]

    def append(self, session_id: str, actor_id: str, turns: list[dict]) -> None:
        self.store.get_or_create_session(session_id, actor_id=actor_id).append_items(turns)


def merge_history(stored: list[dict], incoming: list[dict]) -> list[dict]:
    """Use stored turns only when the caller sent just the new question."""
    return stored + incoming if stored and len(incoming) <= 1 else incoming
