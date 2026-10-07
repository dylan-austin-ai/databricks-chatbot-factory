"""One SQL interface for jobs (Spark) and the app/agent (SQL warehouse).

All user-supplied values go through named parameters, never string formatting.
Identifiers (catalog/schema/table names) are validated and backtick-quoted.
"""
from __future__ import annotations

import re
import time
from typing import Any, Protocol

_IDENT = re.compile(r"^[A-Za-z0-9_]+$")


def ident(*parts: str) -> str:
    """Quote a dotted identifier after validating each part."""
    out = []
    for p in parts:
        if not _IDENT.match(p or ""):
            raise ValueError(f"Unsafe identifier: {p!r}")
        out.append(f"`{p}`")
    return ".".join(out)


class SqlRunner(Protocol):
    def execute(self, statement: str, params: dict[str, Any] | None = None) -> None: ...
    def query(self, statement: str, params: dict[str, Any] | None = None) -> list[dict]: ...


class SparkRunner:
    """Used inside Databricks jobs."""

    def __init__(self, spark):
        self.spark = spark

    def execute(self, statement, params=None):
        self.spark.sql(statement, args=params or {})

    def query(self, statement, params=None):
        return [r.asDict(recursive=True) for r in self.spark.sql(statement, args=params or {}).collect()]


class WarehouseRunner:
    """Used by the app and the agent, via the Statement Execution API. Every statement carries
    query tags (component, source, bot_id when known) so warehouse cost is attributable in
    system.query.history (CST-10)."""

    def __init__(self, workspace_client, warehouse_id: str, timeout_s: int = 120,
                 tags: dict[str, str] | None = None):
        self.w = workspace_client
        self.warehouse_id = warehouse_id
        self.timeout_s = timeout_s
        self.tags = {"component": "chatbot-factory", **(tags or {})}

    def tagged(self, **tags) -> "WarehouseRunner":
        return WarehouseRunner(self.w, self.warehouse_id, self.timeout_s, {**self.tags, **tags})

    def _run(self, statement, params) -> dict:
        body = {"warehouse_id": self.warehouse_id, "statement": statement, "wait_timeout": "30s",
                "parameters": [{"name": k, "value": None if v is None else str(v)} for k, v in (params or {}).items()],
                "query_tags": [{"key": k, "value": str(v)} for k, v in self.tags.items() if v]}
        resp = self.w.api_client.do("POST", "/api/2.0/sql/statements", body=body)
        deadline = time.time() + self.timeout_s
        while resp["status"]["state"] in ("PENDING", "RUNNING"):
            if time.time() > deadline:
                self.w.api_client.do("POST", f"/api/2.0/sql/statements/{resp['statement_id']}/cancel")
                raise TimeoutError("SQL statement timed out")
            time.sleep(1)
            resp = self.w.api_client.do("GET", f"/api/2.0/sql/statements/{resp['statement_id']}")
        status = resp["status"]
        if status["state"] != "SUCCEEDED":
            msg = (status.get("error") or {}).get("message") or status["state"]
            raise PermissionError(msg) if "PERMISSION" in msg.upper() else RuntimeError(msg)
        return resp

    def execute(self, statement, params=None):
        self._run(statement, params)

    def query(self, statement, params=None):
        resp = self._run(statement, params)
        result = resp.get("result") or {}
        rows = list(result.get("data_array") or [])
        while result.get("next_chunk_internal_link"):  # large results arrive in chunks
            result = self.w.api_client.do("GET", result["next_chunk_internal_link"])
            rows += result.get("data_array") or []
        cols = [c["name"] for c in resp["manifest"]["schema"]["columns"]] if rows else []
        return [dict(zip(cols, row)) for row in rows]
