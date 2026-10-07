"""Synthetic availability probe running inside the always-on Databricks App (OBS-1).

Every minute it checks, without calling the LLM, that the shared agent endpoint and
the AI Search endpoint are up, and writes a small JSON record to the restricted logs
volume through the Files API. No SQL warehouse or job compute is woken; the monitor
job loads the records and computes availability over 15-minute windows.
"""
from __future__ import annotations

import io
import json
import threading
import time
from datetime import datetime, timezone


def _check_endpoint(w, name: str) -> tuple[bool, str]:
    ep = w.serving_endpoints.get(name)
    ready = getattr(ep.state.ready, "value", str(ep.state.ready))
    return ready == "READY", ready


def _check_ai_search(w, name: str) -> tuple[bool, str]:
    resp = w.api_client.do("GET", f"/api/2.0/vector-search/endpoints/{name}")
    state = (resp.get("endpoint_status") or {}).get("state", "")
    return state == "ONLINE", state


def run_once(w, settings) -> list[dict]:
    out = []
    for check, fn, target in (("endpoint", _check_endpoint, settings.get("agent.serving_endpoint")),
                              ("ai_search", _check_ai_search, settings.get("ai_search.endpoint"))):
        started = time.time()
        try:
            ok, detail = fn(w, target)
        except Exception as e:  # noqa: BLE001
            ok, detail = False, repr(e)[:300]
        out.append({"ts": datetime.now(timezone.utc).isoformat(), "check_name": check, "bot_id": None,
                    "ok": ok, "latency_ms": int((time.time() - started) * 1000), "detail": detail})
    now = datetime.now(timezone.utc)
    path = (f"/Volumes/{settings.catalog}/{settings.platform_schema}/logs/health/"
            f"date={now:%Y-%m-%d}/{now:%H%M%S}.json")
    w.files.upload(path, io.BytesIO("\n".join(json.dumps(r) for r in out).encode()), overwrite=True)
    return out


def start(w, settings) -> threading.Thread:
    interval = int(settings.get("monitoring.probe_interval_seconds", 60))

    def loop():
        while True:
            try:
                run_once(w, settings)
            except Exception:  # noqa: BLE001 - a failed write shows up as missing data
                pass
            time.sleep(interval)

    t = threading.Thread(target=loop, name="synthetic-probe", daemon=True)
    t.start()
    return t
