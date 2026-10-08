"""Agent endpoint housekeeping after a deploy (ARC-1).

`agents.deploy` adds each new model version to the endpoint and leaves the earlier ones in
place. Every served version keeps compute running (the endpoint doesn't scale to zero), and the
endpoint refuses a 16th. So once the new version is serving, the others are removed.
"""
from __future__ import annotations

from datetime import timedelta


def wait_until_serving(w, endpoint: str, model_name: str, version, wait_minutes: int = 45) -> None:
    """Block until the endpoint has finished updating, then check it serves `version`. Raises if
    the update failed or the version isn't being served, so nothing is removed or recorded for
    a deployment that didn't take."""
    details = w.serving_endpoints.wait_get_serving_endpoint_not_updating(
        endpoint, timeout=timedelta(minutes=wait_minutes))
    served = {str(e.entity_version) for e in ((details.config.served_entities if details.config else None) or [])
              if e.entity_name == model_name}
    if str(version) not in served:
        raise RuntimeError(f"Endpoint {endpoint} finished updating but is not serving {model_name} "
                           f"version {version} (serving: {sorted(served) or 'nothing'}).")


def remove_old_versions(w, agents, endpoint: str, model_name: str, keep_version,
                        wait_minutes: int = 45) -> list[str]:
    """Remove every deployed version of the agent except `keep_version`; returns what was
    removed. Call after wait_until_serving. Each removal is an endpoint update, so the endpoint
    is awaited between removals."""
    removed = []
    for deployment in agents.get_deployments(model_name):
        old = str(deployment.model_version)
        if old == str(keep_version):
            continue
        # Always pass the version: without it delete_deployment deletes the whole endpoint.
        agents.delete_deployment(model_name, model_version=int(old))
        w.serving_endpoints.wait_get_serving_endpoint_not_updating(
            endpoint, timeout=timedelta(minutes=wait_minutes))
        removed.append(old)
    return removed
