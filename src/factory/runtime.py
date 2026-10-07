"""Runtime files the shared agent reads instead of querying a SQL warehouse.

Keeping the warehouse out of the request path matters for cost: a serverless warehouse
woken by every question would stay up all business day. The app and jobs republish
these small JSON files whenever a bot's config, state or release changes.

    /Volumes/<catalog>/_platform/runtime/settings.json
    /Volumes/<catalog>/_platform/runtime/bots/<bot_id>.json
"""
from __future__ import annotations

import io
import json


def runtime_dir(settings) -> str:
    return f"/Volumes/{settings.catalog}/{settings.platform_schema}/runtime"


def logs_dir(settings) -> str:
    return f"/Volumes/{settings.catalog}/{settings.platform_schema}/logs"


def _release(cp, release_id: str | None) -> dict | None:
    if not release_id:
        return None
    rows = cp.sql.query(
        f"""SELECT release_id, config_version, config_hash, manifest_hash, golden_hash,
                   parser_hash, platform_version, git_commit, status, model_id
            FROM {cp.s.fq('releases')} WHERE release_id = :r""", {"r": release_id})
    return rows[0] if rows else None


def bot_runtime(cp, bot_id: str) -> dict:
    row = cp.get_bot(bot_id)
    if not row:
        raise KeyError(bot_id)
    return {
        "bot_id": bot_id,
        "state": row["state"],
        "deleted": row.get("deleted_at") is not None,
        "config_version": row["config_version"],
        "config": json.loads(row["config_json"]),
        "live_release": _release(cp, row.get("live_release_id")),
        "candidate_release": _release(cp, row.get("candidate_release_id")),
    }


def _put(w, path: str, payload: dict) -> None:
    w.files.upload(path, io.BytesIO(json.dumps(payload, default=str).encode()), overwrite=True)


def publish_bot(cp, w, bot_id: str) -> None:
    _put(w, f"{runtime_dir(cp.s)}/bots/{bot_id}.json", bot_runtime(cp, bot_id))


def publish_settings(cp, w) -> None:
    _put(w, f"{runtime_dir(cp.s)}/settings.json", cp.load_overrides())

