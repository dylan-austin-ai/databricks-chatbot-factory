"""One SQL interface for jobs (Spark) and the app/agent (SQL warehouse).

All user-supplied values go through named parameters, never string formatting.
Identifiers (catalog/schema/table names) are validated and backtick-quoted.
"""
from __future__ import annotations

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
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


_SQL_TYPE = re.compile(r"^(STRING|INT|BIGINT|DOUBLE|BOOLEAN|ARRAY<(STRING|INT)>)$")


def json_rows(rows: list[dict], schema: dict[str, str], name: str = "rows") -> tuple[str, dict[str, str]]:
    """Many rows in one statement. Returns a FROM-able subquery that yields one row per dict, and
    the single parameter that carries them as JSON, so a batch is one round trip instead of one
    per row. `schema` maps column to type; values must already be that type (an INT as a number,
    not text). Malformed input fails the statement instead of silently inserting nothing."""
    for col, kind in schema.items():
        if not _IDENT.match(col) or not _SQL_TYPE.match(kind):
            raise ValueError(f"Unsafe column for a batch: {col!r} {kind!r}")
    if not _IDENT.match(name):
        raise ValueError(f"Unsafe parameter name: {name!r}")
    struct = ", ".join(f"{col}: {kind}" for col, kind in schema.items())
    payload = json.dumps([{col: row.get(col) for col in schema} for row in rows], default=str)
    return (f"(SELECT inline(from_json(:{name}, 'ARRAY<STRUCT<{struct}>>', map('mode', 'FAILFAST'))))",
            {name: payload})


def in_list(values: list, prefix: str = "v") -> tuple[str, dict[str, str]]:
    """`(:v0, :v1, ...)` and its parameters, for `WHERE id IN ...` in an UPDATE or DELETE. Those
    statements take a plain list; a subquery there is subject to restrictions that vary by
    runtime."""
    if not _IDENT.match(prefix):
        raise ValueError(f"Unsafe parameter prefix: {prefix!r}")
    params = {f"{prefix}{i}": value for i, value in enumerate(values)}
    return "(" + ", ".join(f":{name}" for name in params) + ")", params


def union_by_key(selects: dict[str, str], key: str = "bot_id") -> tuple[str, dict[str, str]]:
    """The same SELECT against several chatbots' tables as one statement instead of one round
    trip each. `selects` maps the key value (a bot_id) to its SELECT; every row comes back with
    the key it belongs to. The SELECTs must return the same columns."""
    if not _IDENT.match(key):
        raise ValueError(f"Unsafe column name: {key!r}")
    parts, params = [], {}
    for i, (value, select) in enumerate(selects.items()):
        params[f"k{i}"] = value
        parts.append(f"SELECT :k{i} AS {key}, x.* FROM ({select}) x")
    return "\nUNION ALL\n".join(parts), params


def run_lanes(sql, lanes: list[list[str]], workers: int = 8) -> None:
    """Run groups of statements that don't depend on each other. Statements inside a lane run in
    order; lanes run side by side when the runner allows it (the SQL warehouse does, a Spark
    session is left alone). Keep everything that touches one object in one lane.

    If a lane stops, what is left of it is run again in order afterwards: a statement can fail
    only because a neighbouring lane was changing related permissions at the same moment, and a
    real failure then raises as usual."""
    lanes = [lane for lane in lanes if lane]
    if not getattr(sql, "parallel", False) or len(lanes) < 2:
        for lane in lanes:
            for statement in lane:
                sql.execute(statement)
        return

    def run(lane: list[str]) -> list[str]:
        for i, statement in enumerate(lane):
            try:
                sql.execute(statement)
            except Exception:  # noqa: BLE001 - retried in order below
                return lane[i:]
        return []

    with ThreadPoolExecutor(max_workers=min(workers, len(lanes))) as pool:
        left_over = list(pool.map(run, lanes))
    for lane in left_over:
        for statement in lane:
            sql.execute(statement)


class SqlRunner(Protocol):
    def execute(self, statement: str, params: dict[str, Any] | None = None) -> None: ...
    def query(self, statement: str, params: dict[str, Any] | None = None) -> list[dict]: ...


class SparkRunner:
    """Used inside Databricks jobs."""

    parallel = False

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

    parallel = True   # each statement is its own HTTP request, so independent ones can overlap

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
