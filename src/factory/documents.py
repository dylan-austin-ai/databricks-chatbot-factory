"""Document lifecycle: register, version, approve, flag, archive, restore,
override, hard delete (DOC-1..13, QA-4, QA-9, QA-11, ING-2)."""
from __future__ import annotations

import io
import json
import uuid

from .config import PlatformSettings
from .controlplane import ControlPlane
from .ingestion import BotPaths
from .validation import ValidationResult, validate_file

FLAG_REASONS = ["Missing text", "Garbled table", "Wrong pages", "Contains sensitive info", "Other"]


class Documents:
    def __init__(self, sql, settings: PlatformSettings, cp: ControlPlane, workspace_client=None):
        self.sql, self.s, self.cp, self.w = sql, settings, cp, workspace_client

    def existing_hashes(self, bot_id: str) -> dict[str, str]:
        p = BotPaths(self.s, bot_id)
        rows = self.sql.query(  # every version ever uploaded counts as a duplicate (DCL-7)
            f"SELECT content_hash, doc_name FROM {p.t('manifest')}")
        return {r["content_hash"]: r["doc_name"] for r in rows}

    def find_by_name(self, bot_id: str, doc_name: str) -> dict | None:
        p = BotPaths(self.s, bot_id)
        rows = self.sql.query(
            f"SELECT * FROM {p.t('manifest')} WHERE doc_name = :n AND status <> 'superseded' "
            "ORDER BY doc_version DESC LIMIT 1", {"n": doc_name})
        return rows[0] if rows else None

    def register(self, bot_id: str, file_name: str, data: bytes, actor: str,
                 source: str = "upload", source_ref: str = "",
                 expires_at: str | None = None,
                 no_expiry: bool = True, effective_date: str | None = None,
                 doc_owner: str | None = None, security_scope: str = "general",
                 geo_scope: str = "ALL") -> ValidationResult:
        """Validate, store in the volume, and add to the manifest. A file with
        the same name as an existing doc becomes a new version (DOC-4)."""
        res = validate_file(file_name, data, self.s.get("ingestion"), self.existing_hashes(bot_id))
        if not res.ok:
            self.cp.audit(actor, bot_id, "upload_rejected", file_name, {"reasons": res.reasons})
            return res
        p = BotPaths(self.s, bot_id)
        prior = self.find_by_name(bot_id, file_name)
        doc_id = prior["doc_id"] if prior else uuid.uuid4().hex[:16]
        version = int(prior["doc_version"]) + 1 if prior else 1
        path = f"{p.files}/{doc_id}_v{version}.{res.ext}"
        if self.w is not None:
            self.w.files.upload(path, io.BytesIO(data), overwrite=True)
        if prior:
            self.sql.execute(
                f"UPDATE {p.t('manifest')} SET status = 'superseded', is_active = false, "
                "updated_at = current_timestamp() WHERE doc_id = :d AND doc_version = :v",
                {"d": doc_id, "v": int(prior["doc_version"])})
        if prior and expires_at is None and no_expiry is True and not prior.get("no_expiry", True):
            # A new version inherits the previous version's expiry choice by default.
            no_expiry = bool(prior.get("no_expiry"))
        self.sql.execute(
            f"""INSERT INTO {p.t('manifest')} (doc_id, doc_version, doc_name, file_path, file_ext,
                content_hash, size_bytes, page_count, source, source_ref, uploaded_by, uploaded_at,
                parser, status, is_active, doc_owner, effective_date, expires_at, no_expiry,
                security_scope, geo_scope, updated_at)
                VALUES (:d, CAST(:v AS INT), :n, :path, :ext, :h, CAST(:sz AS BIGINT),
                CAST(:pc AS INT), :src, :ref, :a, current_timestamp(), :parser, 'pending_review',
                false, :ow, CAST(:ef AS DATE), CAST(:ex AS TIMESTAMP), CAST(:ne AS BOOLEAN), :sc, :geo,
                current_timestamp())""",
            {"d": doc_id, "v": version, "n": file_name, "path": path, "ext": res.ext,
             "h": res.content_hash, "sz": res.size_bytes, "pc": res.page_count,
             "src": source, "ref": source_ref, "a": actor, "parser": res.parser,
             "ow": doc_owner or (prior or {}).get("doc_owner") or actor,
             "ef": effective_date, "ex": None if no_expiry else expires_at,
             "ne": bool(no_expiry or not expires_at), "sc": security_scope, "geo": geo_scope})
        self.cp.audit(actor, bot_id, "doc_uploaded", doc_id,
                      {"name": file_name, "version": version, "source": source})
        return res

    def upload_many(self, bot_id: str, files: list[dict], actor: str) -> list[dict]:
        """Register several files and report on every one of them, so nothing is dropped
        silently. Each entry of `files` has `name`, `data` and any other `register` arguments.
        Returns one row per file: name, ok, reasons, and duplicate (already stored, so nothing
        was lost). A file that fails for a technical reason is reported and recorded too, and
        the rest of the batch still goes ahead."""
        report = []
        for f in files:
            extra = {k: v for k, v in f.items() if k not in ("name", "data")}
            try:
                res = self.register(bot_id, f["name"], f["data"], actor, **extra)
                report.append({"name": f["name"], "ok": res.ok, "reasons": list(res.reasons),
                               "duplicate": bool(res.duplicate_of)})
            except Exception as e:  # noqa: BLE001 - one bad file must not hide the others
                reason = f"We couldn't store this file ({type(e).__name__}). Please try again."
                self.cp.audit(actor, bot_id, "upload_failed", f["name"], {"reasons": [reason]})
                report.append({"name": f["name"], "ok": False, "reasons": [reason], "duplicate": False})
        return report

    def not_uploaded(self, bot_id: str, days: int = 30) -> list[dict]:
        """Files someone tried to add in the last `days` that are still not in the chatbot: the
        attempt was refused or failed and no file of that name has been stored since. Exact
        duplicates are left out, because that content is already there."""
        p = BotPaths(self.s, bot_id)
        rows = self.sql.query(
            f"""SELECT a.ts, a.actor, a.target AS file_name, a.detail_json
                FROM {self.s.fq('audit_log')} a
                WHERE a.bot_id = :b AND a.action IN ('upload_rejected', 'upload_failed')
                  AND a.ts >= current_timestamp() - INTERVAL {int(days)} DAYS
                  AND NOT EXISTS (SELECT 1 FROM {p.t('manifest')} m
                                  WHERE m.doc_name = a.target AND m.uploaded_at > a.ts)
                ORDER BY a.ts DESC""", {"b": bot_id})
        out, seen = [], set()
        for r in rows:
            reasons = (json.loads(r.get("detail_json") or "{}") or {}).get("reasons", [])
            if r["file_name"] in seen or (reasons and all("exact duplicate" in x for x in reasons)):
                continue
            seen.add(r["file_name"])  # newest attempt per file
            out.append({"file_name": r["file_name"], "when": r["ts"], "by": r["actor"], "reasons": reasons})
        return out

    def _set_status(self, bot_id: str, doc_id: str, status: str, active: bool, actor: str,
                    action: str, reason: str | None = None) -> None:
        p = BotPaths(self.s, bot_id)
        self.sql.execute(
            f"UPDATE {p.t('manifest')} SET status = :s, is_active = CAST(:a AS BOOLEAN), "
            "flag_reason = :r, updated_at = current_timestamp() "
            "WHERE doc_id = :d AND status <> 'superseded'",
            {"s": status, "a": active, "r": reason, "d": doc_id})
        self.cp.audit(actor, bot_id, action, doc_id, {"reason": reason} if reason else {})

    def approve(self, bot_id, doc_id, actor):
        p = BotPaths(self.s, bot_id)
        row = self.sql.query(f"SELECT flag_reason FROM {p.t('manifest')} WHERE doc_id = :d "
                             "AND status <> 'superseded'", {"d": doc_id})
        if row and row[0]["flag_reason"] == "Contains sensitive info":
            raise PermissionError("This document contains restricted data and can't be approved. "
                                  "Upload a clean copy instead (ING-8).")
        self._set_status(bot_id, doc_id, "approved", True, actor, "doc_approved")

    def bulk_approve_green(self, bot_id, actor) -> int:
        p = BotPaths(self.s, bot_id)
        rows = self.sql.query(f"SELECT doc_id FROM {p.t('manifest')} WHERE status = 'pending_review' "
                              "AND readability IN ('green', 'n/a')")
        for r in rows:
            self.approve(bot_id, r["doc_id"], actor)
        return len(rows)

    def flag(self, bot_id, doc_id, actor, reason):
        self._set_status(bot_id, doc_id, "flagged", False, actor, "doc_flagged", reason)

    def archive(self, bot_id, doc_id, actor):
        self._set_status(bot_id, doc_id, "archived", False, actor, "doc_archived")

    def restore(self, bot_id, doc_id, actor):
        self._set_status(bot_id, doc_id, "approved", True, actor, "doc_restored")

    def flag_chunk(self, bot_id, chunk_id, actor, flagged=True):
        p = BotPaths(self.s, bot_id)
        self.sql.execute(f"UPDATE {p.t('chunked')} SET flagged = CAST(:f AS BOOLEAN) WHERE chunk_id = :c",
                         {"f": flagged, "c": chunk_id})
        self.cp.audit(actor, bot_id, "chunk_flagged" if flagged else "chunk_unflagged", chunk_id)

    def update_metadata(self, bot_id: str, doc_id: str, actor: str, **fields) -> None:
        """Metadata-only change (owner, dates, scopes): no re-parse or re-chunk (DCL-3).
        Takes effect in the candidate release on the next publish and reaches users on
        promotion (REL-5)."""
        allowed = {"doc_owner": "STRING", "effective_date": "DATE", "expires_at": "TIMESTAMP",
                   "no_expiry": "BOOLEAN", "security_scope": "STRING", "geo_scope": "STRING"}
        sets, params = [], {"d": doc_id}
        for k, v in fields.items():
            if k not in allowed:
                raise ValueError(f"Can't change {k} here.")
            sets.append(f"{k} = CAST(:{k} AS {allowed[k]})")
            params[k] = v
        if not sets:
            return
        if fields.get("no_expiry") is True:
            sets.append("expires_at = NULL")
        p = BotPaths(self.s, bot_id)
        self.sql.execute(f"UPDATE {p.t('manifest')} SET {', '.join(sets)}, updated_at = current_timestamp() "
                         "WHERE doc_id = :d AND status <> 'superseded'", params)
        self.cp.audit(actor, bot_id, "doc_metadata_changed", doc_id, {k: str(v) for k, v in fields.items()})

    def download(self, bot_id: str, doc_id: str) -> tuple[str, bytes]:
        """Source file for users with access (DCL-5). Callers check access first."""
        p = BotPaths(self.s, bot_id)
        row = self.sql.query(f"SELECT doc_name, file_path FROM {p.t('manifest')} WHERE doc_id = :d "
                             "AND status <> 'superseded'", {"d": doc_id})[0]
        return row["doc_name"], self.w.files.download(row["file_path"]).contents.read()

    def set_page_range(self, bot_id, doc_id, page_range: str | None, actor):
        """Re-parse with pages excluded (QA-11). Format: '1-3,5,7-40'."""
        import re
        if page_range and not re.fullmatch(r"\d+(-\d+)?(,\d+(-\d+)?)*", page_range.replace(" ", "")):
            raise ValueError("Use page numbers like 1-3,5,7-40.")
        p = BotPaths(self.s, bot_id)
        self.sql.execute(
            f"UPDATE {p.t('manifest')} SET page_range = :r, updated_at = current_timestamp() "
            "WHERE doc_id = :d AND status <> 'superseded'",
            {"r": page_range.replace(" ", "") if page_range else None, "d": doc_id})
        self.cp.audit(actor, bot_id, "doc_page_range_set", doc_id, {"page_range": page_range})

    def save_override(self, bot_id, doc_id, text, actor):
        """Manual text correction (QA-11); takes priority over parser output."""
        p = BotPaths(self.s, bot_id)
        v = self.sql.query(f"SELECT doc_version FROM {p.t('manifest')} WHERE doc_id = :d "
                           "AND status <> 'superseded'", {"d": doc_id})[0]["doc_version"]
        self.sql.execute(
            f"INSERT INTO {p.t('parsed_elements')} VALUES (:d, CAST(:v AS INT), 'manual_override', "
            "NULL, :t, true, current_timestamp())", {"d": doc_id, "v": v, "t": text})
        self.cp.audit(actor, bot_id, "doc_override_saved", doc_id)

    def hard_delete(self, bot_id, doc_id, actor, is_admin: bool, legal_reason: str):
        """Admin only, for legal or retention requests (DOC-10)."""
        if not is_admin:
            raise PermissionError("Only MLOps admins can permanently delete documents.")
        p = BotPaths(self.s, bot_id)
        rows = self.sql.query(f"SELECT file_path FROM {p.t('manifest')} WHERE doc_id = :d", {"d": doc_id})
        for t in ("chunked", "parsed_elements", "manifest"):
            self.sql.execute(f"DELETE FROM {p.t(t)} WHERE doc_id = :d", {"d": doc_id})
        if self.w is not None:
            for r in rows:
                try:
                    self.w.files.delete(r["file_path"])
                except Exception:  # noqa: BLE001
                    pass
        self.cp.audit(actor, bot_id, "doc_hard_deleted", doc_id, {"reason": legal_reason})
