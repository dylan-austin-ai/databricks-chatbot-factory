"""Idempotent provisioning (PRV-1..7, PRV-11, GOV-3, CST-1/2, ARC-4).

Each step records its status in provisioning_steps; a retry skips completed
steps and resumes where it failed. Every step is also safe to re-run.
"""
from __future__ import annotations

import io
from datetime import timedelta
from typing import Callable

from .config import BotConfig, PlatformSettings
from .controlplane import ControlPlane, render
from .ingestion import BotPaths
from .sql import SqlRunner, ident


def principal(name: str) -> str:
    if not name or "`" in name:
        raise ValueError(f"Unsafe principal name: {name!r}")
    return f"`{name}`"


def _tag_value(v: str) -> str:
    return (v or "unassigned").replace("'", "")[:255]


class Provisioner:
    STEPS = [
        "create_objects", "tag_objects", "grant_access", "write_config",
        "ensure_index", "create_alerts", "create_trigger",
    ]

    def __init__(self, sql: SqlRunner, settings: PlatformSettings, cp: ControlPlane,
                 workspace_client=None, vector_client=None, agent_principal: str | None = None,
                 ingest_job_id: str | None = None):
        self.sql, self.s, self.cp = sql, settings, cp
        self.w = workspace_client
        self.vsc = vector_client
        self.agent_principal = agent_principal
        self.ingest_job_id = ingest_job_id

    # Step bookkeeping ---------------------------------------------------
    def _done_steps(self, bot_id: str) -> set[str]:
        rows = self.sql.query(
            f"""SELECT step FROM {self.s.fq('provisioning_steps')} WHERE bot_id = :b
                QUALIFY ROW_NUMBER() OVER (PARTITION BY step ORDER BY updated_at DESC) = 1
                AND status = 'done'""", {"b": bot_id})
        return {r["step"] for r in rows}

    def _record(self, bot_id: str, step: str, status: str, detail: str = "") -> None:
        self.sql.execute(
            f"INSERT INTO {self.s.fq('provisioning_steps')} VALUES (:b, :s, :st, :d, current_timestamp())",
            {"b": bot_id, "s": step, "st": status, "d": detail[:4000]})

    def run(self, cfg: BotConfig, actor: str, force: bool = False,
            progress: Callable[[str], None] = lambda m: None,
            only: list[str] | None = None) -> None:
        done = set() if force else self._done_steps(cfg.bot_id)
        for step in self.STEPS:
            if step in done or (only and step not in only):
                continue
            progress(PROGRESS_MESSAGES.get(step, step))
            try:
                getattr(self, step)(cfg)
                self._record(cfg.bot_id, step, "done")
            except Exception as e:  # noqa: BLE001 - recorded and re-raised
                self._record(cfg.bot_id, step, "failed", repr(e))
                self.cp.audit(actor, cfg.bot_id, "provision_failed", step, {"error": repr(e)})
                raise
        self.cp.audit(actor, cfg.bot_id, "provisioned", ",".join(only or self.STEPS), {})

    # Steps --------------------------------------------------------------
    def create_trigger(self, cfg: BotConfig) -> None:
        """DOC-14: a per-bot copy of the ingest job with a file-arrival trigger on the bot's files
        folder, so ingestion starts within about a minute of any upload or git sync."""
        if not (self.w and self.ingest_job_id and self.s.get("ingestion.auto_trigger", True)):
            return
        from databricks.sdk.service import jobs as j
        from .ingestion import BotPaths
        base = self.w.jobs.get(int(self.ingest_job_id)).settings
        name = f"[factory] auto-ingest {cfg.bot_id}"
        new = j.JobSettings(
            name=name, tasks=base.tasks, environments=base.environments, max_concurrent_runs=1,
            job_clusters=base.job_clusters, git_source=base.git_source,
            usage_policy_id=getattr(base, "usage_policy_id", None),
            tags={**(base.tags or {}), self.s.get("tags.chatbot_name"): cfg.bot_id},
            parameters=[j.JobParameterDefinition(name=p.name, default=cfg.bot_id if p.name == "bot_id" else p.default)
                        for p in base.parameters or []],
            trigger=j.TriggerSettings(pause_status=j.PauseStatus.UNPAUSED, file_arrival=j.FileArrivalTriggerConfiguration(
                url=BotPaths(self.s, cfg.bot_id).files + "/", min_time_between_triggers_seconds=60,
                wait_after_last_change_seconds=60)))
        found = list(self.w.jobs.list(name=name))
        if found:
            self.w.jobs.reset(found[0].job_id, new_settings=new)
        else:
            self.w.jobs.create(**{k: v for k, v in new.__dict__.items() if v is not None})

    def remove_trigger(self, bot_id: str) -> None:
        for job in self.w.jobs.list(name=f"[factory] auto-ingest {bot_id}") if self.w else []:
            self.w.jobs.delete(job.job_id)

    def create_objects(self, cfg: BotConfig) -> None:
        for stmt in render("bot_schema_ddl.sql", catalog=ident(self.s.catalog),
                           schema=ident(cfg.bot_id),
                           display_name=cfg.display_name.replace("'", "")):
            self.sql.execute(stmt)

    def tag_objects(self, cfg: BotConfig) -> None:
        """Everything unique to the bot carries the bot's tags (CST-2)."""
        t = self.s.get("tags")
        tags = (f"'{t['team']}' = '{_tag_value(cfg.team or cfg.owner_group)}', "
                f"'{t['function']}' = '{_tag_value(cfg.business_function)}', "
                f"'{t['chatbot_name']}' = '{_tag_value(cfg.bot_id)}'")
        schema = ident(self.s.catalog, cfg.bot_id)
        self.sql.execute(f"ALTER SCHEMA {schema} SET TAGS ({tags})")
        self.sql.execute(f"ALTER VOLUME {schema}.`docs` SET TAGS ({tags})")
        for table in ("manifest", "parsed_elements", "chunked", "index_source", "golden_set"):
            self.sql.execute(f"ALTER TABLE {schema}.{ident(table)} SET TAGS ({tags})")

    def grant_access(self, cfg: BotConfig) -> None:
        """Bot access = UC grants on the bot's schema (GOV-3). Users never get MODIFY on
        bot infrastructure; the app's service principal owns it (LCY-5, REL-10).

        access_probe: may use the live bot. tester_probe: may use the candidate release
        (owner, owner group, Reviewer, up to 10 testers; REL-1)."""
        cat = ident(self.s.catalog)
        schema = ident(self.s.catalog, cfg.bot_id)
        app_sp = self.s.get("access.app_service_principal")
        if app_sp:
            self.sql.execute(f"ALTER SCHEMA {schema} OWNER TO {principal(app_sp)}")
        owners = {cfg.owner_group, cfg.owner_user, cfg.reviewer}
        testers = owners | set(cfg.testers)
        if self.agent_principal:
            owners.add(self.agent_principal)
        users = set(cfg.allowed_principals) if cfg.access_mode == "restricted" else \
            set(self.s.get("access.trusted_callers", []))
        for p in sorted(x for x in owners | testers | users if x):
            self.sql.execute(f"GRANT USE CATALOG ON CATALOG {cat} TO {principal(p)}")
            self.sql.execute(f"GRANT USE SCHEMA ON SCHEMA {schema} TO {principal(p)}")
        for p in sorted(x for x in owners | users if x):
            self.sql.execute(f"GRANT SELECT ON TABLE {schema}.`access_probe` TO {principal(p)}")
        for p in sorted(x for x in testers if x):
            self.sql.execute(f"GRANT SELECT ON TABLE {schema}.`tester_probe` TO {principal(p)}")
        for p in sorted(x for x in owners if x):
            for table in ("manifest", "chunked", "golden_set"):
                self.sql.execute(f"GRANT SELECT ON TABLE {schema}.{ident(table)} TO {principal(p)}")
            self.sql.execute(f"GRANT READ VOLUME ON VOLUME {schema}.`docs` TO {principal(p)}")
        for probe in ("access_probe", "tester_probe"):
            self.sql.execute(f"INSERT INTO {schema}.{ident(probe)} SELECT true "
                             f"WHERE NOT EXISTS (SELECT 1 FROM {schema}.{ident(probe)})")

    def set_testers(self, cfg: BotConfig, new_testers: list[str], actor: str) -> None:
        """Invite or remove candidate testers (max 10, REL-1)."""
        from .config import MAX_TESTERS
        new = [t.strip() for t in new_testers if t.strip()]
        if len(new) > MAX_TESTERS:
            raise ValueError(f"You can invite up to {MAX_TESTERS} testers.")
        schema = ident(self.s.catalog, cfg.bot_id)
        keep = {cfg.owner_group, cfg.owner_user, cfg.reviewer}
        for t in sorted(set(cfg.testers) - set(new) - keep):
            self.sql.execute(f"REVOKE SELECT ON TABLE {schema}.`tester_probe` FROM {principal(t)}")
        for t in sorted(set(new) - set(cfg.testers)):
            self.sql.execute(f"GRANT USE CATALOG ON CATALOG {ident(self.s.catalog)} TO {principal(t)}")
            self.sql.execute(f"GRANT USE SCHEMA ON SCHEMA {schema} TO {principal(t)}")
            self.sql.execute(f"GRANT SELECT ON TABLE {schema}.`tester_probe` TO {principal(t)}")
        cfg.testers = new
        self.cp.save_config(cfg, actor, "testers updated")

    def write_config(self, cfg: BotConfig) -> None:
        if self.w is None:
            return
        p = BotPaths(self.s, cfg.bot_id)
        self.w.files.upload(p.config_file, io.BytesIO(cfg.to_yaml().encode()), overwrite=True)

    def ensure_index(self, cfg: BotConfig) -> None:
        """Confidential bots get a dedicated index; others use the shared one."""
        if not cfg.dedicated_index or self.vsc is None:
            return
        name = f"{self.s.catalog}.{cfg.bot_id}.chunks_index"
        existing = {i.get("name") for i in self.vsc.list_indexes(
            self.s.get("ai_search.endpoint")).get("vector_indexes", [])}
        if name in existing:
            return
        self.vsc.create_delta_sync_index(
            endpoint_name=self.s.get("ai_search.endpoint"), index_name=name,
            source_table_name=f"{self.s.catalog}.{cfg.bot_id}.index_source",
            pipeline_type="TRIGGERED", primary_key="chunk_id",
            embedding_source_column="chunk_to_embed",
            embedding_model_endpoint_name=self.s.get("models.embedding_endpoint"),
            columns_to_sync=INDEX_COLUMNS,
        )

    def create_alerts(self, cfg: BotConfig) -> None:
        """Email alerts for the owner (OBS-5). Never blocks provisioning."""
        if self.w is None:
            return
        try:
            from .alerting import ensure_bot_alerts
            ensure_bot_alerts(self.w, self.s, cfg, self.s.get("warehouse_id"))
        except Exception as e:  # noqa: BLE001
            self.cp.alert(cfg.bot_id, "alert_setup_failed", "medium",
                          f"Email alerts could not be set up automatically ({e}); MLOps will set them up.")


INDEX_COLUMNS = ["chunk_id", "bot_id", "doc_id", "doc_version", "doc_name", "source_uri",
                 "page_ids", "section", "chunk_to_retrieve", "doc_type",
                 "effective_date", "department", "in_live", "in_candidate",
                 "effective_ts", "expires_ts", "security_scope", "geo_scope"]

PROGRESS_MESSAGES = {
    "create_objects": "Creating your chatbot's secure workspace…",
    "tag_objects": "Labeling everything for cost tracking…",
    "grant_access": "Setting up who can use your chatbot…",
    "write_config": "Saving your answers…",
    "ensure_index": "Preparing search…",
    "create_alerts": "Setting up email alerts…",
    "create_trigger": "Turning on automatic reading of new documents…",
}


def grant_agent_access(sql: SqlRunner, settings: PlatformSettings, agent_principal: str) -> None:
    """What the agent's service principal needs at runtime: read its config, write its logs."""
    if not agent_principal:
        return
    platform, who = ident(settings.catalog, settings.platform_schema), principal(agent_principal)
    sql.execute(f"GRANT USE CATALOG ON CATALOG {ident(settings.catalog)} TO {who}")
    sql.execute(f"GRANT USE SCHEMA ON SCHEMA {platform} TO {who}")
    sql.execute(f"GRANT READ VOLUME ON VOLUME {platform}.`runtime` TO {who}")
    sql.execute(f"GRANT WRITE VOLUME ON VOLUME {platform}.`logs` TO {who}")
    sql.execute(f"GRANT SELECT ON TABLE {platform}.`user_access` TO {who}")


def ensure_shared_index(vsc, settings: PlatformSettings, wait_minutes: int = 60,
                        usage_policy_id: str = "") -> str:
    """Platform setup: one AI Search endpoint + one shared index (ARC-4).

    Endpoint and index creation are asynchronous, so each is awaited before the next step uses
    it: the index is only created on an online endpoint, and this returns the index name only
    once the index exists in Unity Catalog and can be queried.
    """
    timeout = timedelta(minutes=wait_minutes)
    ep = settings.get("ai_search.endpoint")
    if ep not in {e.get("name") for e in vsc.list_endpoints().get("endpoints", [])}:
        # The usage policy puts the endpoint's cost under this environment's tags (CST-11).
        policy = {"usage_policy_id": usage_policy_id} if usage_policy_id else {}
        vsc.create_endpoint(name=ep, endpoint_type="STANDARD", **policy)
    vsc.wait_for_endpoint(ep, timeout=timeout)
    name = f"{settings.catalog}.{settings.platform_schema}.{settings.get('ai_search.shared_index')}"
    existing = {i.get("name") for i in vsc.list_indexes(ep).get("vector_indexes", [])}
    if name not in existing:
        vsc.create_delta_sync_index(
            endpoint_name=ep, index_name=name,
            source_table_name=settings.fq("shared_chunks"), pipeline_type="TRIGGERED",
            primary_key="chunk_id", embedding_source_column="chunk_to_embed",
            embedding_model_endpoint_name=settings.get("models.embedding_endpoint"),
            columns_to_sync=INDEX_COLUMNS,
        )
    vsc.get_index(ep, name).wait_until_ready(timeout=timeout)
    return name


def index_name_for(settings: PlatformSettings, cfg: BotConfig) -> str:
    if cfg.dedicated_index:
        return f"{settings.catalog}.{cfg.bot_id}.chunks_index"
    return f"{settings.catalog}.{settings.platform_schema}.{settings.get('ai_search.shared_index')}"


def sync_index(vsc, settings: PlatformSettings, cfg: BotConfig) -> None:
    """Triggered sync right after publish, so archives take effect now (DOC-8)."""
    idx = vsc.get_index(settings.get("ai_search.endpoint"), index_name_for(settings, cfg))
    idx.sync()

