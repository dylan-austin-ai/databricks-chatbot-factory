"""Ingestion orchestration used by the provision, ingest, re-index and sync jobs.

parse -> chunk -> injection scan (DCL-8) -> extraction QA (+ optional visual judge)
-> PII scan -> auto-approve greens -> optional golden-set generation
-> stage a candidate release (REL-1) -> sync index.

Nothing here touches what end users see: changes land in the candidate release and
reach users only after Reviewer approval and promotion (REL-5).
"""
from __future__ import annotations

import json
import uuid

from . import guardrails, llm, qa, sensitive
from .config import BotConfig, PlatformSettings
from .controlplane import ControlPlane
from .guardrails import platform_rules
from .ingestion import BotPaths, chunk_sql, doc_params, parse_binary_sql, parse_text_sql, qa_metrics_sql
from .provisioning import sync_index
from .releases import Releases
from .sql import SqlRunner

MAX_VISUAL_PAGES = 30          # per doc, keeps day-one visual judge cost bounded
GOLDEN_SURPLUS = 1.3           # generate extra so 50 survive owner review (EVG-5)
GOLDEN_TEXT_CHARS = 60_000
OUT_OF_SCOPE_COUNT = 10


def _in(n: int) -> str:
    return ", ".join(f":d{i}" for i in range(n))


class Pipeline:
    def __init__(self, sql: SqlRunner, settings: PlatformSettings, cp: ControlPlane,
                 workspace_client, vector_client, log=print):
        self.sql, self.s, self.cp = sql, settings, cp
        self.w, self.vsc, self.log = workspace_client, vector_client, log
        self._llm = None

    @property
    def llm(self):
        if self._llm is None:
            self._llm = llm.openai_client(self.w)
        return self._llm

    # Selection ------------------------------------------------------------
    def docs_needing_parse(self, p: BotPaths) -> list[dict]:
        return self.sql.query(f"""
            SELECT m.doc_id, m.doc_version, m.parser, m.page_count, m.doc_name, m.file_path, m.page_range
            FROM {p.t('manifest')} m
            LEFT ANTI JOIN {p.t('parsed_elements')} pe
              ON pe.doc_id = m.doc_id AND pe.doc_version = m.doc_version
            WHERE m.status NOT IN ('archived', 'superseded')""")

    # Run ------------------------------------------------------------------
    def run(self, cfg: BotConfig, doc_ids: list[str] | None = None,
            reparse: bool = False, generate_golden: bool | None = None,
            rechunk_only: bool = False, actor: str = "system:pipeline") -> dict:
        p = BotPaths(self.s, cfg.bot_id)
        if doc_ids and reparse:
            targets = self.sql.query(
                f"SELECT doc_id, doc_version, parser, page_count, doc_name, file_path, page_range "
                f"FROM {p.t('manifest')} "
                f"WHERE doc_id IN ({_in(len(doc_ids))}) AND status <> 'superseded'",
                doc_params(doc_ids))
        else:
            targets = self.docs_needing_parse(p)
            if doc_ids:
                targets = [t for t in targets if t["doc_id"] in set(doc_ids)]
        ids = [t["doc_id"] for t in targets]
        if ids:
            self.log(f"Processing {len(ids)} document(s)")
            params = doc_params(ids)
            if not rechunk_only:  # rechunk_only: manual override saved (QA-11)
                # One statement per document version: the parser's options must be constants.
                for t in targets:
                    if t["parser"] == "ai_parse_document":
                        self.sql.execute(
                            parse_binary_sql(p, t["doc_id"], t["doc_version"], t["file_path"], t.get("page_range")),
                            {"d": t["doc_id"], "v": int(t["doc_version"])})
                if any(t["parser"] == "text_reader" for t in targets):
                    self.sql.execute(parse_text_sql(p, len(ids)), params)
            for stmt in chunk_sql(p, self.s, len(ids)):
                self.sql.execute(stmt, params)
            injected = self.injection_scan(p, ids)
            self.extraction_qa(cfg, p, targets, injected)
            gen = (cfg.golden_set_mode == "generate") if generate_golden is None else generate_golden
            if gen:
                self.generate_golden(cfg, p, ids)
        release_id = self.publish(cfg, actor)
        return {"processed": ids, "release_id": release_id}

    def _current_chunks(self, p: BotPaths, doc_ids: list[str]) -> list[dict]:
        return self.sql.query(f"""
            SELECT c.chunk_id, c.doc_id, c.chunk_to_retrieve FROM {p.t('chunked')} c
            JOIN {p.t('manifest')} m ON m.doc_id = c.doc_id AND m.doc_version = c.doc_version
            WHERE c.doc_id IN ({_in(len(doc_ids))}) AND m.status <> 'superseded'""", doc_params(doc_ids))

    # Prompt-injection scan at ingestion (DCL-8) -------------------------------
    def injection_scan(self, p: BotPaths, doc_ids: list[str]) -> dict[str, int]:
        hits: dict[str, list[str]] = {}
        for c in self._current_chunks(p, doc_ids):
            if guardrails.find_injection(c["chunk_to_retrieve"] or ""):
                hits.setdefault(c["doc_id"], []).append(c["chunk_id"])
        for doc_id, chunk_ids in hits.items():
            self.sql.execute(
                f"UPDATE {p.t('chunked')} SET flagged = true, injection_flag = true "
                f"WHERE chunk_id IN ({', '.join(f':c{i}' for i in range(len(chunk_ids)))})",
                {f"c{i}": c for i, c in enumerate(chunk_ids)})
        return {d: len(c) for d, c in hits.items()}

    # QA -------------------------------------------------------------------
    def extraction_qa(self, cfg: BotConfig, p: BotPaths, targets: list[dict],
                      injected: dict[str, int] | None = None) -> None:
        ids = [t["doc_id"] for t in targets]
        expected = {t["doc_id"]: t.get("page_count") for t in targets}
        rows = self.sql.query(qa_metrics_sql(p, len(ids)), doc_params(ids))
        thresholds = self.s.get("quality.readability")
        auto_approve = self.s.get("quality.review_policy", "auto_approve_green") == "auto_approve_green"
        for row in rows:
            result = qa.classify(row, thresholds, expected.get(row["doc_id"]))
            if cfg.visual_judge and not row.get("is_text"):
                try:
                    result = qa.merge_visual(result, self.visual_judge(p, row["doc_id"]))
                except Exception as e:  # noqa: BLE001 - the check is optional; the detail is for MLOps
                    self.log(f"Visual check failed for {row['doc_id']}: {type(e).__name__}: {str(e)[:500]}")
                    result.messages.append("Visual check unavailable.")
            n_inj = (injected or {}).get(row["doc_id"], 0)
            if n_inj:
                result.messages.append(
                    f"{n_inj} section(s) contain text that looks like instructions to an AI. They were "
                    "held back; review them under Sections and unflag any that are fine.")
                if result.badge in ("green", "n/a"):
                    result.badge = "yellow"
            scan = self._scan(p, row["doc_id"])
            result.messages += scan.messages()
            if scan.blocked:  # restricted data never reaches the index (ING-8)
                result.badge, status, active, reason = "red", "flagged", False, "Contains sensitive info"
            else:
                status = "approved" if (auto_approve and result.badge in ("green", "n/a")) else "pending_review"
                active, reason = True, None
            self.sql.execute(
                f"""UPDATE {p.t('manifest')} SET readability = :b, qa_json = :q, status = :s,
                    is_active = CAST(:a AS BOOLEAN), flag_reason = :r, updated_at = current_timestamp()
                    WHERE doc_id = :d AND status NOT IN ('archived', 'superseded')""",
                {"b": result.badge, "q": result.to_json(), "s": status, "a": active, "r": reason,
                 "d": row["doc_id"]})
            if status == "approved":
                self.cp.audit("system:auto_approve", cfg.bot_id, "doc_approved", row["doc_id"],
                              {"badge": result.badge})
            elif scan.blocked:
                self.cp.audit("system:pii_scan", cfg.bot_id, "doc_blocked_sensitive", row["doc_id"],
                              {"found": scan.restricted})

    def _scan(self, p: BotPaths, doc_id: str):
        text = " ".join(c["chunk_to_retrieve"] or "" for c in self._current_chunks(p, [doc_id]))
        return sensitive.scan(text)

    def _current_version(self, p: BotPaths, doc_id: str) -> int:
        return self.sql.query(f"SELECT doc_version FROM {p.t('manifest')} WHERE doc_id = :d "
                              "AND status <> 'superseded'", {"d": doc_id})[0]["doc_version"]

    def _page_texts(self, p: BotPaths, doc_id: str) -> dict[int, str]:
        rows = self.sql.query(f"""
            SELECT e.value:bbox[0]:page_id::INT AS page, e.value:content::STRING AS content
            FROM {p.t('parsed_elements')} pe, LATERAL variant_explode(pe.parsed:document:elements) e
            WHERE pe.doc_id = :d AND pe.doc_version = :v AND pe.parsed IS NOT NULL
            QUALIFY DENSE_RANK() OVER (ORDER BY pe.parsed_at DESC) = 1""",
            {"d": doc_id, "v": self._current_version(p, doc_id)})
        pages: dict[int, list[str]] = {}
        for r in rows:
            pages.setdefault(r["page"] or 0, []).append(r["content"] or "")
        return {k: "\n".join(v) for k, v in pages.items()}

    def visual_judge(self, p: BotPaths, doc_id: str) -> list[dict]:
        """QA-6: Sonnet compares each page image with its extracted text."""
        folder = f"{p.images}/{doc_id}/v{self._current_version(p, doc_id)}"
        images = sorted(f.path for f in self.w.files.list_directory_contents(folder)
                        if not f.is_directory)
        texts = self._page_texts(p, doc_id)
        findings: list[dict] = []
        for page_idx, path in enumerate(images[:MAX_VISUAL_PAGES]):
            img = self.w.files.download(path).contents.read()
            out = llm.chat_json(self.llm, self.s.get("models.generation_endpoint"),
                                qa.VISUAL_JUDGE_PROMPT.format(page=page_idx + 1,
                                                              text=texts.get(page_idx, "")[:12000]),
                                images=[img], max_tokens=800)
            findings += out.get("findings", [])
        return findings

    # Golden set (EVL-1, EVL-4, EVG-4, QA-1) -----------------------------------
    def _doc_text_with_pages(self, p: BotPaths, doc_id: str) -> str:
        rows = self.sql.query(f"""
            SELECT c.chunk_to_retrieve, c.page_ids FROM {p.t('chunked')} c
            JOIN {p.t('manifest')} m ON m.doc_id = c.doc_id AND m.doc_version = c.doc_version
            WHERE c.doc_id = :d AND m.status <> 'superseded' ORDER BY c.chunk_position""", {"d": doc_id})
        parts = []
        for r in rows:
            pages = r["page_ids"] or []
            if isinstance(pages, str):
                pages = json.loads(pages or "[]")
            label = ", ".join(str(int(x) + 1) for x in pages) or "?"
            parts.append(f"[page {label}]\n{r['chunk_to_retrieve'] or ''}")
        return "\n\n".join(parts)

    def generate_golden(self, cfg: BotConfig, p: BotPaths, doc_ids: list[str]) -> None:
        docs = self.sql.query(f"""SELECT doc_id, doc_name FROM {p.t('manifest')}
                                  WHERE doc_id IN ({_in(len(doc_ids))}) AND status <> 'superseded'""",
                              doc_params(doc_ids))
        judge = self.s.get("models.generation_endpoint")
        # Spread the in-scope target across the bot's docs, half of them hard (EVG-4).
        n_docs = int(self.sql.query(f"SELECT count(*) AS n FROM {p.t('manifest')} "
                                    "WHERE status <> 'superseded' AND is_active")[0]["n"] or 1)
        target = self.s.get("quality.min_golden_questions", 50) * GOLDEN_SURPLUS - OUT_OF_SCOPE_COUNT
        per_doc = max(2, -(-int(target) // n_docs))
        for d in docs:
            out = llm.chat_json(self.llm, judge, qa.GOLDEN_SET_PROMPT.format(
                purpose=cfg.purpose, n_easy=per_doc - per_doc // 2, n_hard=per_doc // 2,
                doc_name=d["doc_name"],
                text=self._doc_text_with_pages(p, d["doc_id"])[:GOLDEN_TEXT_CHARS]), max_tokens=4000)
            for q in out.get("questions", []):
                chunk = self._chunk_for_quote(p, d["doc_id"], q.get("quote", ""))
                self._insert_golden(p, q["question"], q.get("expected_answer", ""), d["doc_id"], chunk,
                                    "in_scope", "generated", q.get("expected_pages") or [],
                                    q.get("difficulty", "easy"), q.get("question_type", "fact"))
            issues = out.get("extraction_issues") or []
            if issues:
                self._append_qa_messages(p, d["doc_id"], [f"Reviewer note: {i}" for i in issues])
        existing_oos = self.sql.query(
            f"SELECT count(*) AS n FROM {p.t('golden_set')} WHERE kind = 'out_of_scope'")[0]["n"]
        if int(existing_oos or 0) == 0:
            out = llm.chat_json(self.llm, judge, qa.OUT_OF_SCOPE_PROMPT.format(
                purpose=cfg.purpose, refuse="; ".join(cfg.refuse_topics + platform_rules(
                    self.s.get("guardrails.platform_rules", {}), cfg.disabled_rules)) or "none",
                n=OUT_OF_SCOPE_COUNT))
            for q in out.get("questions", []):
                self._insert_golden(p, q, "", None, None, "out_of_scope", "generated", [], "easy",
                                    "out_of_scope")

    def _chunk_for_quote(self, p: BotPaths, doc_id: str, quote: str) -> str | None:
        if not quote:
            return None
        rows = self.sql.query(
            f"""SELECT c.chunk_id FROM {p.t('chunked')} c
                JOIN {p.t('manifest')} m ON m.doc_id = c.doc_id AND m.doc_version = c.doc_version
                WHERE c.doc_id = :d AND m.status <> 'superseded'
                AND contains(lower(regexp_replace(c.chunk_to_retrieve, '\\\\s+', ' ')),
                             lower(regexp_replace(:q, '\\\\s+', ' '))) LIMIT 1""",
            {"d": doc_id, "q": quote[:300]})
        return rows[0]["chunk_id"] if rows else None

    def _insert_golden(self, p, question, answer, doc_id, chunk_id, kind, origin,
                       expected_pages, difficulty, question_type) -> None:
        self.sql.execute(
            f"""INSERT INTO {p.t('golden_set')} (eval_id, question, expected_answer, source_doc_id,
                source_chunk_id, expected_pages, kind, difficulty, question_type, origin, approved,
                active, updated_by, updated_at)
                VALUES (:id, :q, :a, :d, :c, from_json(:pg, 'ARRAY<INT>'), :k, :df, :qt, :o, false, true,
                'system:generator', current_timestamp())""",
            {"id": str(uuid.uuid4()), "q": question, "a": answer, "d": doc_id, "c": chunk_id,
             "pg": json.dumps([int(x) for x in expected_pages if str(x).isdigit()]), "k": kind,
             "df": difficulty, "qt": question_type, "o": origin})

    def _append_qa_messages(self, p: BotPaths, doc_id: str, msgs: list[str]) -> None:
        row = self.sql.query(f"SELECT qa_json FROM {p.t('manifest')} WHERE doc_id = :d "
                             "AND status <> 'superseded'", {"d": doc_id})
        if not row:
            return
        data = json.loads(row[0]["qa_json"] or "{}")
        data.setdefault("messages", []).extend(msgs)
        self.sql.execute(f"UPDATE {p.t('manifest')} SET qa_json = :q WHERE doc_id = :d "
                         "AND status <> 'superseded'", {"q": json.dumps(data), "d": doc_id})

    # Stage the candidate release (REL-1, DCL-4) ---------------------------------------
    def publish(self, cfg: BotConfig, actor: str = "system:pipeline") -> str:
        rid = Releases(self.sql, self.s, self.cp).stage(cfg, actor)
        if self.vsc is not None:
            sync_index(self.vsc, self.s, cfg)
        self.cp.publish(cfg.bot_id)
        self.log(f"Staged candidate release {rid} and triggered index sync")
        return rid
