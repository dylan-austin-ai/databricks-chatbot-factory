"""Release channels inside prod (REL-1..11).

Every chunk row in the index source carries two flags:

    in_live       visible to end users
    in_candidate  visible to the owner, Reviewer and up to 10 testers

Unchanged chunks have both flags, so they are embedded once. Staging a change only
flips flags on rows that actually change (no full-table churn, so no re-embedding of
unchanged rows). Promotion makes the candidate set live without re-parsing or
re-embedding; rollback re-activates a previous release from its recorded membership.

index_history records when each chunk was live (with its expiry window), so the live
index can be reconstructed for any date (REL-4).
"""
from __future__ import annotations

import hashlib
import json
import os

from . import PLATFORM_VERSION
from .config import BotConfig, PlatformSettings
from .controlplane import ControlPlane
from .ingestion import PARSER_VERSION, BotPaths, approved_chunks_select
from .sql import SqlRunner, ident


class IntegrityError(RuntimeError):
    """Raised when the search source fails integrity checks (DCL-4)."""


def content_hash(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()


def index_table(settings: PlatformSettings, cfg: BotConfig) -> str:
    if cfg.dedicated_index:
        return ident(settings.catalog, cfg.bot_id, "index_source")
    return ident(settings.catalog, settings.platform_schema, "shared_chunks")


META_COLS = ["effective_ts", "expires_ts", "security_scope", "geo_scope"]


class Releases:
    def __init__(self, sql: SqlRunner, settings: PlatformSettings, cp: ControlPlane):
        self.sql, self.s, self.cp = sql, settings, cp

    # Integrity (DCL-4) ----------------------------------------------------------
    def integrity_problems(self, p: BotPaths) -> list[str]:
        approved = approved_chunks_select(p)
        rows = self.sql.query(f"""
            SELECT count(*) AS n, count(DISTINCT chunk_id) AS n_ids,
                   count_if(coalesce(length(trim(chunk_to_embed)), 0) = 0) AS empty_embed,
                   count_if(coalesce(length(trim(chunk_to_retrieve)), 0) = 0) AS empty_retrieve
            FROM ({approved})""")
        r = rows[0] if rows else {"n": 0, "n_ids": 0, "empty_embed": 0, "empty_retrieve": 0}
        problems = []
        if int(r["n"] or 0) != int(r["n_ids"] or 0):
            problems.append("Duplicate section IDs found; the index was not updated.")
        if int(r["empty_embed"] or 0) or int(r["empty_retrieve"] or 0):
            problems.append("Some sections have no text; the index was not updated.")
        return problems

    # Release records --------------------------------------------------------------
    def _bot(self, bot_id: str) -> dict:
        row = self.cp.get_bot(bot_id)
        if not row:
            raise KeyError(bot_id)
        return row

    def open_candidate(self, cfg: BotConfig, actor: str) -> str:
        bot = self._bot(cfg.bot_id)
        if bot.get("candidate_release_id"):
            return bot["candidate_release_id"]
        seq_row = self.sql.query(
            f"SELECT coalesce(max(seq), 0) AS s FROM {self.s.fq('releases')} WHERE bot_id = :b",
            {"b": cfg.bot_id})
        seq = int(seq_row[0]["s"] if seq_row else 0) + 1
        rid = f"{cfg.bot_id}-r{seq:04d}"
        self.sql.execute(
            f"""INSERT INTO {self.s.fq('releases')} (release_id, bot_id, seq, status, platform_version,
                git_commit, environment, submitted_by, created_at, updated_at)
                VALUES (:r, :b, CAST(:s AS INT), 'candidate', :pv, :gc, :env, :a,
                current_timestamp(), current_timestamp())""",
            {"r": rid, "b": cfg.bot_id, "s": seq, "pv": PLATFORM_VERSION,
             "gc": os.environ.get("FACTORY_GIT_COMMIT", ""), "env": self.s.get("environment"),
             "a": actor})
        self.sql.execute(f"UPDATE {self.s.fq('bots')} SET candidate_release_id = :r WHERE bot_id = :b",
                         {"r": rid, "b": cfg.bot_id})
        self.cp.audit(actor, cfg.bot_id, "release_opened", rid)
        return rid

    def snapshots(self, cfg: BotConfig, p: BotPaths) -> dict[str, tuple[str, object]]:
        """Versioned, hashed snapshots of config, manifest, golden set, parser (REL-9)."""
        config = {"bot": json.loads(cfg.to_json()),
                  "agent": {"temperature": self.s.get("agent.temperature"),
                            "max_output_tokens": self.s.get("agent.max_output_tokens"),
                            "answer_service": self.s.answer_service(cfg.answer_model)}}
        manifest = self.sql.query(f"""
            SELECT doc_id, doc_version, doc_name, content_hash, doc_owner,
                   CAST(effective_date AS STRING) AS effective_date, CAST(expires_at AS STRING) AS expires_at,
                   no_expiry, security_scope, geo_scope, page_range
            FROM {p.t('manifest')} WHERE status = 'approved' AND is_active ORDER BY doc_id""")
        golden = self.sql.query(f"""
            SELECT eval_id, question, expected_answer, source_doc_id, source_chunk_id,
                   expected_pages, kind, difficulty, question_type
            FROM {p.t('golden_set')} WHERE approved AND active ORDER BY eval_id""")
        parser = {"parser_version": PARSER_VERSION,
                  "metadata_schema": self.s.get("ingestion.metadata_schema"),
                  "page_ranges": {m["doc_id"]: m["page_range"] for m in manifest if m.get("page_range")}}
        return {k: (content_hash(v), v) for k, v in
                {"config": config, "manifest": manifest, "golden": golden, "parser": parser}.items()}

    # Stage (REL-1) ------------------------------------------------------------------
    def stage(self, cfg: BotConfig, actor: str) -> str:
        p = BotPaths(self.s, cfg.bot_id)
        problems = self.integrity_problems(p)
        if problems:
            self.cp.alert(cfg.bot_id, "integrity", "high", " ".join(problems))
            raise IntegrityError(" ".join(problems))
        rid = self.open_candidate(cfg, actor)
        tbl = index_table(self.s, cfg)
        approved = approved_chunks_select(p)
        params = {"b": cfg.bot_id}
        meta_differs = " OR ".join(f"NOT (t.{c} <=> s.{c})" for c in META_COLS)
        # Only rows whose flag or (candidate-only) metadata changes are touched.
        self.sql.execute(f"""
            MERGE INTO {tbl} t USING ({approved}) s
            ON t.chunk_id = s.chunk_id AND t.bot_id = s.bot_id
            WHEN MATCHED AND NOT coalesce(t.in_candidate, false) THEN
              UPDATE SET in_candidate = true, updated_at = current_timestamp()
            WHEN MATCHED AND NOT coalesce(t.in_live, false) AND ({meta_differs}) THEN
              UPDATE SET effective_ts = s.effective_ts, expires_ts = s.expires_ts,
                security_scope = s.security_scope, geo_scope = s.geo_scope,
                doc_name = s.doc_name, updated_at = current_timestamp()
            WHEN NOT MATCHED THEN INSERT (chunk_id, bot_id, doc_id, doc_version, doc_name,
                source_uri, page_ids, section, chunk_to_retrieve, chunk_to_embed, doc_type,
                effective_date, department, in_live, in_candidate, effective_ts, expires_ts,
                security_scope, geo_scope, updated_at)
              VALUES (s.chunk_id, s.bot_id, s.doc_id, s.doc_version, s.doc_name, s.source_uri,
                s.page_ids, s.section, s.chunk_to_retrieve, s.chunk_to_embed, s.doc_type,
                s.effective_date, s.department, false, true, s.effective_ts, s.expires_ts,
                s.security_scope, s.geo_scope, current_timestamp())
            WHEN NOT MATCHED BY SOURCE AND t.bot_id = :b AND coalesce(t.in_candidate, false) THEN
              UPDATE SET in_candidate = false, updated_at = current_timestamp()""", params)
        self.sql.execute(f"DELETE FROM {tbl} WHERE bot_id = :b AND NOT coalesce(in_live, false) "
                         "AND NOT coalesce(in_candidate, false)", params)
        # Membership + metadata of this release, for promotion and rollback (REL-3)
        self.sql.execute(f"DELETE FROM {self.s.fq('release_chunks')} WHERE release_id = :r", {"r": rid})
        self.sql.execute(f"""
            INSERT INTO {self.s.fq('release_chunks')}
            SELECT :r, bot_id, chunk_id, doc_id, doc_version, effective_ts, expires_ts,
                   security_scope, geo_scope FROM ({approved})""", {"r": rid})
        snaps = self.snapshots(cfg, p)
        self.sql.execute(f"DELETE FROM {self.s.fq('snapshots')} WHERE release_id = :r", {"r": rid})
        for kind, (h, content) in snaps.items():
            self.sql.execute(
                f"INSERT INTO {self.s.fq('snapshots')} VALUES (:r, :b, :k, :h, :c, current_timestamp())",
                {"r": rid, "b": cfg.bot_id, "k": kind, "h": h, "c": json.dumps(content, default=str)})
        counts = self.sql.query(f"""SELECT count(DISTINCT doc_id) AS docs, count(*) AS chunks
                                    FROM {self.s.fq('release_chunks')} WHERE release_id = :r""", {"r": rid})
        bot = self._bot(cfg.bot_id)
        self.sql.execute(
            f"""UPDATE {self.s.fq('releases')} SET config_version = CAST(:cv AS INT), config_hash = :ch,
                manifest_hash = :mh, golden_hash = :gh, parser_hash = :ph,
                doc_count = CAST(:dc AS INT), chunk_count = CAST(:cc AS INT),
                eval_run_id = NULL, eval_passed = NULL, safety_override_by = NULL,
                safety_override_reason = NULL, updated_at = current_timestamp()
                WHERE release_id = :r""",
            {"r": rid, "cv": bot["config_version"], "ch": snaps["config"][0], "mh": snaps["manifest"][0],
             "gh": snaps["golden"][0], "ph": snaps["parser"][0],
             "dc": counts[0]["docs"] if counts else 0, "cc": counts[0]["chunks"] if counts else 0})
        self.cp.audit(actor, cfg.bot_id, "release_staged", rid,
                      {"manifest_hash": snaps["manifest"][0], "config_hash": snaps["config"][0]})
        return rid

    # Promote (REL-2, REL-5) ------------------------------------------------------------
    def _sync_history(self, bot_id: str, release_id: str) -> None:
        """Close history rows that leave live (or change expiry/scope); open new ones."""
        rc = self.s.fq("release_chunks")
        hist = self.s.fq("index_history")
        same = " AND ".join(f"h.{c} <=> r.{c}" for c in META_COLS)
        self.sql.execute(f"""
            UPDATE {hist} h SET live_to = current_timestamp()
            WHERE h.bot_id = :b AND h.live_to IS NULL AND NOT EXISTS (
              SELECT 1 FROM {rc} r WHERE r.release_id = :r AND r.chunk_id = h.chunk_id AND {same})""",
                         {"b": bot_id, "r": release_id})
        self.sql.execute(f"""
            INSERT INTO {hist}
            SELECT r.bot_id, r.chunk_id, r.doc_id, r.doc_version, r.release_id, current_timestamp(), NULL,
                   r.effective_ts, r.expires_ts, r.security_scope, r.geo_scope
            FROM {rc} r WHERE r.release_id = :r AND NOT EXISTS (
              SELECT 1 FROM {hist} h WHERE h.bot_id = r.bot_id AND h.chunk_id = r.chunk_id
                AND h.live_to IS NULL AND {same})""", {"r": release_id})

    def _apply_live(self, cfg: BotConfig, release_id: str, keep_candidate: bool) -> None:
        """Make the index's live set equal the release's membership and metadata."""
        tbl = index_table(self.s, cfg)
        p = BotPaths(self.s, cfg.bot_id)
        rc = self.s.fq("release_chunks")
        params = {"b": cfg.bot_id, "r": release_id}
        # Re-insert chunks the release needs that are no longer in the index (rollback case).
        self.sql.execute(f"""
            MERGE INTO {tbl} t USING (
              SELECT c.chunk_id, r.bot_id, c.doc_id, c.doc_version, c.doc_name, c.source_uri,
                     c.page_ids, c.section, c.chunk_to_retrieve, c.chunk_to_embed, c.doc_type,
                     c.effective_date, c.department, r.effective_ts, r.expires_ts,
                     r.security_scope, r.geo_scope
              FROM {rc} r JOIN {p.t('chunked')} c ON c.chunk_id = r.chunk_id
              WHERE r.release_id = :r) s
            ON t.chunk_id = s.chunk_id AND t.bot_id = s.bot_id
            WHEN MATCHED AND (NOT coalesce(t.in_live, false)
                 OR NOT (t.effective_ts <=> s.effective_ts) OR NOT (t.expires_ts <=> s.expires_ts)
                 OR NOT (t.security_scope <=> s.security_scope) OR NOT (t.geo_scope <=> s.geo_scope)) THEN
              UPDATE SET in_live = true, effective_ts = s.effective_ts, expires_ts = s.expires_ts,
                security_scope = s.security_scope, geo_scope = s.geo_scope,
                updated_at = current_timestamp()
            WHEN NOT MATCHED THEN INSERT (chunk_id, bot_id, doc_id, doc_version, doc_name, source_uri,
                page_ids, section, chunk_to_retrieve, chunk_to_embed, doc_type, effective_date,
                department, in_live, in_candidate, effective_ts, expires_ts, security_scope, geo_scope,
                updated_at)
              VALUES (s.chunk_id, s.bot_id, s.doc_id, s.doc_version, s.doc_name, s.source_uri,
                s.page_ids, s.section, s.chunk_to_retrieve, s.chunk_to_embed, s.doc_type,
                s.effective_date, s.department, true, false, s.effective_ts, s.expires_ts,
                s.security_scope, s.geo_scope, current_timestamp())
            WHEN NOT MATCHED BY SOURCE AND t.bot_id = :b AND coalesce(t.in_live, false) THEN
              UPDATE SET in_live = false, updated_at = current_timestamp()""", params)
        if not keep_candidate:
            self.sql.execute(f"""UPDATE {tbl} SET in_candidate = in_live, updated_at = current_timestamp()
                                 WHERE bot_id = :b AND NOT (in_candidate <=> in_live)""", params)
        self.sql.execute(f"DELETE FROM {tbl} WHERE bot_id = :b AND NOT coalesce(in_live, false) "
                         "AND NOT coalesce(in_candidate, false)", params)
        self._sync_history(cfg.bot_id, release_id)

    def promote(self, cfg: BotConfig, actor: str) -> str:
        bot = self._bot(cfg.bot_id)
        rid = bot.get("candidate_release_id")
        if not rid:
            raise ValueError("There are no pending changes to publish.")
        self._apply_live(cfg, rid, keep_candidate=False)
        if bot.get("live_release_id"):
            self.sql.execute(f"""UPDATE {self.s.fq('releases')} SET status = 'superseded',
                                 retired_at = current_timestamp() WHERE release_id = :r""",
                             {"r": bot["live_release_id"]})
        self.sql.execute(f"""UPDATE {self.s.fq('releases')} SET status = 'live', approved_by = :a,
                             promoted_at = current_timestamp(), updated_at = current_timestamp()
                             WHERE release_id = :r""", {"r": rid, "a": actor})
        self.sql.execute(f"""UPDATE {self.s.fq('bots')} SET live_release_id = :r,
                             candidate_release_id = NULL WHERE bot_id = :b""", {"r": rid, "b": cfg.bot_id})
        self.cp.audit(actor, cfg.bot_id, "release_promoted", rid)
        return rid

    # Rollback (REL-3) -----------------------------------------------------------------
    def previous_release(self, bot_id: str) -> str | None:
        rows = self.sql.query(f"""SELECT release_id FROM {self.s.fq('releases')}
                                  WHERE bot_id = :b AND status = 'superseded'
                                  ORDER BY promoted_at DESC LIMIT 1""", {"b": bot_id})
        return rows[0]["release_id"] if rows else None

    def rollback(self, cfg: BotConfig, actor: str, to_release: str | None = None, reason: str = "") -> str:
        bot = self._bot(cfg.bot_id)
        target = to_release or self.previous_release(cfg.bot_id)
        if not target:
            raise ValueError("There is no earlier release to roll back to.")
        self._apply_live(cfg, target, keep_candidate=bool(bot.get("candidate_release_id")))
        if bot.get("live_release_id"):
            self.sql.execute(f"""UPDATE {self.s.fq('releases')} SET status = 'rolled_back',
                                 retired_at = current_timestamp(), notes = :n WHERE release_id = :r""",
                             {"r": bot["live_release_id"], "n": reason})
        self.sql.execute(f"""UPDATE {self.s.fq('releases')} SET status = 'live',
                             promoted_at = current_timestamp() WHERE release_id = :r""", {"r": target})
        self.sql.execute(f"UPDATE {self.s.fq('bots')} SET live_release_id = :r WHERE bot_id = :b",
                         {"r": target, "b": cfg.bot_id})
        self.cp.audit(actor, cfg.bot_id, "release_rolled_back", target, {"reason": reason})
        return target

    # Reconstruction (REL-4) --------------------------------------------------------------
    def index_as_of_sql(self) -> str:
        """Chunks that were live and in their effective window at :as_of (a TIMESTAMP)."""
        return f"""
        SELECT h.* FROM {self.s.fq('index_history')} h
        WHERE h.bot_id = :b AND h.live_from <= :as_of
          AND (h.live_to IS NULL OR h.live_to > :as_of)
          AND h.effective_ts <= unix_timestamp(:as_of) AND h.expires_ts > unix_timestamp(:as_of)"""
