"""Control-plane access: settings overrides, bot records, audit log."""
from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path
from typing import Any

from .lifecycle import check_transition
from .config import BotConfig, PlatformSettings
from .problems import brief
from .sql import SqlRunner, ident, in_list, json_rows

SQL_DIR = Path(__file__).resolve().parents[2] / "sql"


def split_statements(sql_text: str) -> list[str]:
    """Split a DDL file into statements on semicolons.

    Semicolons inside quoted strings ('...', "..."), backtick identifiers and comments don't
    end a statement. Line comments (-- ...) are dropped; block comments are kept.
    """
    out, buf, i, n = [], [], 0, len(sql_text)

    def flush() -> None:
        stmt = "".join(buf).strip()  # text inside strings is left exactly as written
        if stmt:
            out.append(stmt)
        buf.clear()

    while i < n:
        ch = sql_text[i]
        if ch in "'\"`":  # quoted run; '' closes and reopens, so doubling needs no special case
            end = i + 1
            while end < n and sql_text[end] != ch:
                end += 2 if sql_text[end] == "\\" and ch != "`" else 1
            buf.append(sql_text[i:end + 1])
            i = end + 1
        elif sql_text.startswith("--", i):
            end = sql_text.find("\n", i)
            i = n if end == -1 else end
        elif sql_text.startswith("/*", i):
            end = sql_text.find("*/", i + 2)
            end = n if end == -1 else end + 2
            buf.append(sql_text[i:end])
            i = end
        elif ch == ";":
            flush()
            i += 1
        else:
            buf.append(ch)
            i += 1
    flush()
    return out


def render(template_name: str, **values: str) -> list[str]:
    text = (SQL_DIR / template_name).read_text()
    return split_statements(text.format(**values))


class ControlPlane:
    def __init__(self, sql: SqlRunner, settings: PlatformSettings, workspace_client=None):
        self.sql = sql
        self.s = settings
        self.w = workspace_client  # when set, runtime JSON is republished on every change

    def publish(self, bot_id: str | None = None) -> None:
        """Republish runtime files the agent reads (no SQL warehouse in the hot path)."""
        if self.w is None:
            return
        from .runtime import publish_bot, publish_settings
        try:
            if bot_id:
                publish_bot(self, self.w, bot_id)
            else:
                publish_settings(self, self.w)
        except Exception as e:  # noqa: BLE001 - never block the user action
            self.alert(bot_id, "runtime_publish_failed", "high", f"Runtime publish failed: {brief(e)}",
                       replace=True)

    # Setup ------------------------------------------------------------------
    # Columns added to tables that may already exist: CREATE TABLE IF NOT EXISTS won't add them.
    MIGRATIONS = {
        "platform_releases": {"git_branch": "STRING", "git_origin": "STRING", "config_json": "STRING"},
    }

    def ensure(self) -> None:
        tags = self.s.get("tags")
        for stmt in render(
            "controlplane_ddl.sql",
            catalog=ident(self.s.catalog), platform=ident(self.s.platform_schema),
            tag_chatbot=tags["chatbot_name"], shared_value=tags["shared_value"],
        ):
            self.sql.execute(stmt)
        for table, columns in self.MIGRATIONS.items():
            have = {str(r.get("col_name", "")).lower() for r in self.sql.query(f"DESCRIBE TABLE {self.s.fq(table)}")}
            missing = [f"{name} {kind}" for name, kind in columns.items() if name.lower() not in have]
            if missing:
                self.sql.execute(f"ALTER TABLE {self.s.fq(table)} ADD COLUMNS ({', '.join(missing)})")

    def deployment_scope(self) -> dict[str, str]:
        """This deployment's environment and workspace ID, as values for SQL templates. The
        account-wide system tables hold every workspace's usage, so cost views filter on these."""
        scope = {"environment": str(self.s.get("environment") or ""),
                 "workspace_id": str(self.w.get_workspace_id()) if self.w is not None else ""}
        for name, value in scope.items():
            if not all(ch.isalnum() or ch in "_-" for ch in value):
                raise ValueError(f"Unsafe {name}: {value!r}")
        return scope

    def ensure_views(self) -> None:
        tags = self.s.get("tags")
        for stmt in render(
            "dashboard_views.sql",
            catalog=ident(self.s.catalog), platform=ident(self.s.platform_schema),
            tag_chatbot=tags["chatbot_name"], shared_value=tags["shared_value"],
            **self.deployment_scope(),
        ):
            self.sql.execute(stmt)

    # Settings (ADM-2) -------------------------------------------------------
    def load_overrides(self) -> dict[str, Any]:
        rows = self.sql.query(
            f"SELECT key, value_json FROM {self.s.fq('platform_settings')} "
            "QUALIFY ROW_NUMBER() OVER (PARTITION BY key ORDER BY updated_at DESC) = 1"
        )
        return {r["key"]: json.loads(r["value_json"]) for r in rows}

    def set_setting(self, key: str, value: Any, actor: str) -> None:
        self.sql.execute(
            f"INSERT INTO {self.s.fq('platform_settings')} VALUES (:k, :v, :a, current_timestamp())",
            {"k": key, "v": json.dumps(value), "a": actor},
        )
        self.audit(actor, None, "setting_changed", key, {"value": value})
        self.publish(None)

    def resolve_alerts(self, bot_id: str | None, kind: str) -> None:
        """Close the open alerts of one kind for one chatbot (the platform's own when bot_id is
        None): what they reported is no longer true, or is being replaced by a newer report."""
        self.sql.execute(
            f"UPDATE {self.s.fq('alerts')} SET resolved = true "
            "WHERE NOT coalesce(resolved, false) AND kind = :k AND bot_id <=> :b", {"k": kind, "b": bot_id})

    def open_alerts(self) -> list[dict]:
        """Alerts that are unresolved and still describe something true, newest first."""
        from .monitoring import split_alerts, unresolved_alerts_sql
        return split_alerts(self.sql.query(unresolved_alerts_sql(self.s)),
                            int(self.s.get("monitoring.expiry_lead_days", 30)))[0]

    def clear_stale_alerts(self) -> int:
        """Close the unresolved alerts that no longer describe anything true (see
        monitoring.alert_is_current). Returns how many were closed."""
        from .monitoring import split_alerts, unresolved_alerts_sql
        stale = split_alerts(self.sql.query(unresolved_alerts_sql(self.s)),
                             int(self.s.get("monitoring.expiry_lead_days", 30)))[1]
        ids = [r["alert_id"] for r in stale if r.get("alert_id")]
        for start in range(0, len(ids), 500):
            listed, params = in_list(ids[start:start + 500], "a")
            self.sql.execute(f"UPDATE {self.s.fq('alerts')} SET resolved = true WHERE alert_id IN {listed}", params)
        if ids:
            self.audit("system:alerts", None, "alerts_cleared", "", {"count": len(ids)})
        return len(ids)

    # Audit (LCY-7, DOC-13) --------------------------------------------------
    def audit(self, actor: str, bot_id: str | None, action: str, target: str = "",
              detail: dict | None = None) -> None:
        self.sql.execute(
            f"INSERT INTO {self.s.fq('audit_log')} VALUES "
            "(:id, current_timestamp(), :actor, :bot, :action, :target, :detail)",
            {"id": str(uuid.uuid4()), "actor": actor, "bot": bot_id, "action": action,
             "target": target, "detail": json.dumps(detail or {})},
        )

    def audit_many(self, actor: str, bot_id: str | None, action: str,
                   items: list[tuple[str, dict | None]]) -> None:
        """One audit row per (target, detail), written in a single statement."""
        if not items:
            return
        source, params = json_rows(
            [{"id": str(uuid.uuid4()), "target": target, "detail": json.dumps(detail or {})}
             for target, detail in items], {"id": "STRING", "target": "STRING", "detail": "STRING"})
        self.sql.execute(
            f"INSERT INTO {self.s.fq('audit_log')} "
            f"SELECT id, current_timestamp(), :actor, :bot, :action, target, detail FROM {source}",
            {**params, "actor": actor, "bot": bot_id, "action": action})

    # Platform releases (REL-7, REL-8) ------------------------------------------
    def record_platform_release(self, platform_version: str, git_commit: str, environment: str,
                                model_version: str, gate_enforced: bool, deployed_by: str,
                                git_branch: str = "", git_origin: str = "", config: dict | None = None) -> str:
        """One row per agent deployment; returns its release_id.

        `config` is what the deployment ran with (catalog, workspace, endpoint, warehouse, usage
        policy and so on); a hash of the effective platform settings is added to it, so two
        deployments can be compared exactly.

        The id is generated here and the row is written with INSERT ... SELECT and named
        columns: Databricks SQL rejects its uuid function inside a parameterized VALUES clause, and naming
        the columns keeps the insert valid if the table gains columns later.
        """
        release_id = str(uuid.uuid4())
        settings_json = json.dumps(self.s.as_dict(), sort_keys=True, default=str)
        config = {**(config or {}), "settings_sha256": hashlib.sha256(settings_json.encode()).hexdigest()}
        self.sql.execute(
            f"INSERT INTO {self.s.fq('platform_releases')} "
            "(release_id, platform_version, git_commit, environment, model_version, gate_enforced, "
            "deployed_by, ts, git_branch, git_origin, config_json) "
            "SELECT :id, :pv, :gc, :env, :mv, CAST(:gate AS BOOLEAN), :who, current_timestamp(), "
            ":branch, :origin, :config",
            {"id": release_id, "pv": platform_version, "gc": git_commit, "env": environment,
             "mv": model_version, "gate": gate_enforced, "who": deployed_by,
             "branch": git_branch, "origin": git_origin, "config": json.dumps(config, sort_keys=True)},
        )
        return release_id

    # Bots ---------------------------------------------------------------
    def existing_bot_ids(self) -> set[str]:
        return {r["bot_id"] for r in self.sql.query(f"SELECT bot_id FROM {self.s.fq('bots')}")}

    def get_bot(self, bot_id: str) -> dict | None:
        rows = self.sql.query(
            f"SELECT * FROM {self.s.fq('bots')} WHERE bot_id = :b", {"b": bot_id}
        )
        return rows[0] if rows else None

    def get_config(self, bot_id: str) -> BotConfig:
        row = self.get_bot(bot_id)
        if not row:
            raise KeyError(bot_id)
        return BotConfig.from_dict(json.loads(row["config_json"]))

    def list_bots(self, include_deleted: bool = False) -> list[dict]:
        where = "" if include_deleted else "WHERE deleted_at IS NULL"
        return self.sql.query(f"SELECT * FROM {self.s.fq('bots')} {where} ORDER BY display_name")

    def save_config(self, cfg: BotConfig, actor: str, reason: str, state: str | None = None) -> int:
        row = self.get_bot(cfg.bot_id)
        version = (int(row["config_version"]) + 1) if row else 1
        params = {
            "b": cfg.bot_id, "n": cfg.display_name, "c": cfg.to_json(), "v": version,
            "s": state or (row["state"] if row else "draft"), "ou": cfg.owner_user,
            "og": cfg.owner_group, "rv": cfg.reviewer, "am": cfg.access_mode,
            "se": cfg.sensitivity, "cr": cfg.critical, "a": actor,
        }
        self.sql.execute(
            f"""MERGE INTO {self.s.fq('bots')} t USING (SELECT :b AS bot_id) s
            ON t.bot_id = s.bot_id
            WHEN MATCHED THEN UPDATE SET display_name = :n, config_json = :c,
              config_version = CAST(:v AS INT), state = :s, owner_user = :ou,
              owner_group = :og, reviewer = :rv, access_mode = :am, sensitivity = :se,
              critical = CAST(:cr AS BOOLEAN), updated_at = current_timestamp()
            WHEN NOT MATCHED THEN INSERT (bot_id, display_name, config_json, config_version,
              state, owner_user, owner_group, reviewer, access_mode, sensitivity, critical,
              created_by, created_at, updated_at)
              VALUES (:b, :n, :c, CAST(:v AS INT), :s, :ou, :og, :rv, :am, :se,
              CAST(:cr AS BOOLEAN), :a, current_timestamp(), current_timestamp())""",
            params,
        )
        self.sql.execute(
            f"INSERT INTO {self.s.fq('bot_versions')} VALUES "
            "(:b, CAST(:v AS INT), :c, :a, current_timestamp(), :r)",
            {"b": cfg.bot_id, "v": version, "c": cfg.to_json(), "a": actor, "r": reason},
        )
        self.audit(actor, cfg.bot_id, "config_saved", f"v{version}", {"reason": reason})
        self.publish(cfg.bot_id)
        return version

    def set_state(self, bot_id: str, state: str, actor: str, detail: dict | None = None) -> None:
        check_transition(self.get_bot(bot_id)["state"], state)
        extra = {"deleted": ", deleted_at = current_timestamp()", "archived": ", deleted_at = NULL"}.get(state, "")
        self.sql.execute(
            f"UPDATE {self.s.fq('bots')} SET state = :s, updated_at = current_timestamp(){extra} "
            "WHERE bot_id = :b",
            {"s": state, "b": bot_id},
        )
        self.audit(actor, bot_id, "state_changed", state, detail)
        self.publish(bot_id)

    def alert(self, bot_id: str | None, kind: str, severity: str, message: str,
              dedupe_key: str | None = None, required: bool = True, replace: bool = False) -> bool:
        """Record an alert; emailed by the bot's SQL alert (OBS-5). Returns False if a
        matching open alert (same dedupe_key) already exists.

        `replace` closes earlier open alerts of this kind for this chatbot first, so a failure
        that happens again is one alert with a fresh time (and one email), not a growing pile.
        Keep `message` short and plain: it is emailed and shown on pages. problems.brief() turns
        an error into one line."""
        if dedupe_key and self.sql.query(
                f"SELECT 1 FROM {self.s.fq('alerts')} WHERE dedupe_key = :d AND NOT coalesce(resolved, false) LIMIT 1",
                {"d": dedupe_key}):
            return False
        if replace:
            self.resolve_alerts(bot_id, kind)
        self.sql.execute(
            f"INSERT INTO {self.s.fq('alerts')} VALUES "
            "(:id, current_timestamp(), :b, :k, :sev, :m, false, :d, CAST(:r AS BOOLEAN))",
            {"id": str(uuid.uuid4()), "b": bot_id, "k": kind, "sev": severity, "m": message,
             "d": dedupe_key, "r": required},
        )
        return True

