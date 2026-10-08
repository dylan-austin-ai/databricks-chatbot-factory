"""Checks for what agent/deploy.py needs but cannot build itself (docs/SETUP.md, Part A).

Everything is checked before the deploy starts and reported together, so a missing piece is
one clear message up front instead of a failure halfway through.
"""
from __future__ import annotations

from typing import Callable

from .config import PlatformSettings


def deploy_problems(w, settings: PlatformSettings, warehouse_id: str, identity_scope: str,
                    agent_sp_scope: str) -> list[str]:
    """Returns one line per missing prerequisite; empty when the deploy can start."""
    problems: list[str] = []

    def need(what: str, fix: str, check: Callable[[], object]) -> None:
        try:
            check()
        except Exception as e:  # noqa: BLE001 - any failure means the deploy can't rely on it
            problems.append(f"{what}: {fix} ({str(e)[:160]})")

    def need_secrets(scope: str, keys: list[str], fix: str) -> None:
        def check() -> None:
            missing = sorted(set(keys) - {x.key for x in w.secrets.list_secrets(scope)})
            if missing:
                raise LookupError(f"missing keys: {', '.join(missing)}")
        need(f"Secret scope '{scope}'", fix, check)

    need(f"Catalog '{settings.catalog}'", "create it and make the deploy identity its owner (A5)",
         lambda: w.catalogs.get(settings.catalog))
    need(f"SQL warehouse '{warehouse_id}'", "create it and pass its ID as warehouse_id (A4)",
         lambda: w.warehouses.get(warehouse_id))
    for key in ("models.judge_endpoint", "models.embedding_endpoint"):
        name = settings.get(key)
        need(f"Serving endpoint '{name}' ({key})", "pick a model your region offers (A1)",
             lambda name=name: w.serving_endpoints.get(name))
    need_secrets(agent_sp_scope, ["agent-sp-client-id", "agent-sp-client-secret"],
                 "store the agent service principal's credentials and give the deploy identity READ (A6)")
    need_secrets(identity_scope,
                 [settings.get("access.identity_hash_key"), settings.get("access.identity_encryption_key")],
                 "store the identity keys and give the deploy identity READ (A7)")
    return problems


def gate_problems(bots: list[dict], platform_version: str, git_commit: str) -> list[str]:
    """Why a gated (prod) deploy may not go ahead; empty when it may.

    `bots` is one row per chatbot in the QA catalog: bot_id, required (live and not pinned to an
    older platform), passed (its latest quality check for this platform version and git commit
    passed). A quality check runs the chatbot's test questions through the deployed agent, so it
    is the end-to-end functional test. Every required chatbot must have passed, and at least one
    chatbot must have passed: an empty QA catalog proves nothing about this build.
    """
    if not git_commit:
        return ["the deployed git commit is unknown; deploy from a git checkout so results can be matched to it"]
    build = f"platform {platform_version} at commit {git_commit[:12]}"
    problems = [f"chatbot '{b['bot_id']}' has not passed its quality check for {build}"
                for b in bots if b.get("required") and not b.get("passed")]
    if not any(b.get("passed") for b in bots):
        problems.append(f"no chatbot in QA has passed an end-to-end quality check for {build}; "
                        "create one in QA and run run_evals there first")
    return problems
