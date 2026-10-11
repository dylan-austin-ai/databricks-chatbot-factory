"""Ingestion orchestration used by the provision, ingest, re-index and sync jobs.

parse -> chunk -> injection scan (DCL-8) -> extraction QA (+ optional visual judge)
-> PII scan -> auto-approve greens -> optional golden-set generation
-> stage a candidate release (REL-1) -> sync index.

Nothing here touches what end users see: changes land in the candidate release and
reach users only after Reviewer approval and promotion (REL-5).
"""
from __future__ import annotations

import json
import re
import uuid
from concurrent.futures import ThreadPoolExecutor

from . import guardrails, llm, qa, sensitive
from .config import BotConfig, PlatformSettings
from .controlplane import ControlPlane
from .guardrails import platform_rules
from .ingestion import (BotPaths, chunk_sql, doc_params, page_images, page_images_sql, page_order,
                        parse_binary_sql, parse_text_sql, qa_metrics_sql)
from .provisioning import sync_index
from .releases import Releases
from .sql import SqlRunner, json_rows

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
                # One statement per document version (the parser's options must be constants). Each
                # is an independent append, and reading is the slow part, so they run side by side.
                self._each([t for t in targets if t["parser"] == "ai_parse_document"], lambda t: self.sql.execute(
                    parse_binary_sql(p, t["doc_id"], t["doc_version"], t["file_path"], t.get("page_range")),
                    {"d": t["doc_id"], "v": int(t["doc_version"])}))
                if any(t["parser"] == "text_reader" for t in targets):
                    self.sql.execute(parse_text_sql(p, len(ids)), params)
            for stmt in chunk_sql(p, self.s, len(ids)):
                self.sql.execute(stmt, params if ":d0" in stmt else None)
            chunks = self._current_chunks(p, ids)   # read once for the injection and data scans
            injected = self.injection_scan(p, ids, chunks)
            self.extraction_qa(cfg, p, targets, injected, chunks)
            gen = (cfg.golden_set_mode == "generate") if generate_golden is None else generate_golden
            if gen:
                self.generate_golden(cfg, p, ids)
        release_id = self.publish(cfg, actor)
        return {"processed": ids, "release_id": release_id}

    def _each(self, items: list, work) -> list:
        """Run `work` over `items`, a few at a time (ingestion.parallel_documents, default 4; 1 turns
        it off). Every item is attempted; the first failure is raised once all have finished."""
        workers = max(1, int(self.s.get("ingestion.parallel_documents", 4) or 1))
        if workers == 1 or len(items) < 2:
            return [work(item) for item in items]

        def attempt(item):
            try:
                return work(item), None
            except Exception as e:  # noqa: BLE001 - raised below, after the others finish
                return None, e

        with ThreadPoolExecutor(max_workers=min(workers, len(items))) as pool:
            results = list(pool.map(attempt, items))
        for _, error in results:
            if error is not None:
                raise error
        return [value for value, _ in results]

    def _current_chunks(self, p: BotPaths, doc_ids: list[str]) -> list[dict]:
        return self.sql.query(f"""
            SELECT c.chunk_id, c.doc_id, c.chunk_to_retrieve FROM {p.t('chunked')} c
            JOIN {p.t('manifest')} m ON m.doc_id = c.doc_id AND m.doc_version = c.doc_version
            WHERE c.doc_id IN ({_in(len(doc_ids))}) AND m.status <> 'superseded'""", doc_params(doc_ids))

    # Prompt-injection scan at ingestion (DCL-8) -------------------------------
    def injection_scan(self, p: BotPaths, doc_ids: list[str], chunks: list[dict] | None = None) -> dict[str, int]:
        hits: dict[str, list[str]] = {}
        for c in chunks if chunks is not None else self._current_chunks(p, doc_ids):
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
                      injected: dict[str, int] | None = None, chunks: list[dict] | None = None) -> None:
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
            scan = self._scan(p, row["doc_id"], chunks)
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

    def _scan(self, p: BotPaths, doc_id: str, chunks: list[dict] | None = None):
        mine = [c for c in chunks if c["doc_id"] == doc_id] if chunks is not None else self._current_chunks(p, [doc_id])
        return sensitive.scan(" ".join(c["chunk_to_retrieve"] or "" for c in mine))

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
            # Page ids can arrive as text ("0"); the visual check looks pages up by number.
            page = int(r["page"]) if r["page"] not in (None, "") else 0
            pages.setdefault(page, []).append(r["content"] or "")
        return {k: "\n".join(v) for k, v in pages.items()}

    def _page_images(self, p: BotPaths, doc_id: str) -> dict[int, str]:
        """Image path per 0-based page, as recorded by the reader for the latest parse. Falls back
        to the folder listing in page order for parses that recorded no image paths."""
        version = self._current_version(p, doc_id)
        images = page_images(self.sql.query(page_images_sql(p), {"d": doc_id, "v": int(version)}))
        if images:
            return images
        folder = f"{p.images}/{doc_id}/v{version}"
        listed = sorted((f.path for f in self.w.files.list_directory_contents(folder)
                         if not f.is_directory), key=page_order)
        return dict(enumerate(listed))

    def visual_judge(self, p: BotPaths, doc_id: str) -> list[dict]:
        """QA-6: Sonnet compares each page image with its extracted text."""
        images = self._page_images(p, doc_id)
        texts = self._page_texts(p, doc_id)
        client, endpoint = self.llm, self.s.get("models.generation_endpoint")

        def check(page: int) -> list[dict]:   # a download and a model call; no shared state
            img = self.w.files.download(images[page]).contents.read()
            out = llm.chat_json(client, endpoint, qa.VISUAL_JUDGE_PROMPT.format(
                page=page + 1, text=texts.get(page, "")[:12000]), images=[img], max_tokens=800)
            return out.get("findings", [])

        return [f for found in self._each(sorted(images)[:MAX_VISUAL_PAGES], check) for f in found]

    def _doc_sections(self, p: BotPaths, doc_ids: list[str]) -> dict[str, list[dict]]:
        """{doc_id: its current sections in order}, for all the documents in one read."""
        rows = self.sql.query(f"""
            SELECT c.doc_id, c.chunk_id, c.chunk_to_retrieve, c.page_ids FROM {p.t('chunked')} c
            JOIN {p.t('manifest')} m ON m.doc_id = c.doc_id AND m.doc_version = c.doc_version
            WHERE c.doc_id IN ({_in(len(doc_ids))}) AND m.status <> 'superseded'
            ORDER BY c.doc_id, c.chunk_position""", doc_params(doc_ids))
        out: dict[str, list[dict]] = {}
        for r in rows:
            out.setdefault(r["doc_id"], []).append(r)
        return out

    @staticmethod
    def _text_with_pages(sections: list[dict]) -> str:
        parts = []
        for r in sections:
            pages = r["page_ids"] or []
            if isinstance(pages, str):
                pages = json.loads(pages or "[]")
            label = ", ".join(str(int(x) + 1) for x in pages) or "?"
            parts.append(f"[page {label}]\n{r['chunk_to_retrieve'] or ''}")
        return "\n\n".join(parts)

    @staticmethod
    def _section_for_quote(sections: list[dict], quote: str) -> str | None:
        """The section a supporting quote comes from: same text once case and spacing are ignored."""
        def norm(text: str) -> str:
            return re.sub(r"\s+", " ", text or "").lower()
        wanted = norm((quote or "")[:300]).strip()
        if not wanted:
            return None
        return next((r["chunk_id"] for r in sections if wanted in norm(r["chunk_to_retrieve"])), None)

    def generate_golden(self, cfg: BotConfig, p: BotPaths, doc_ids: list[str]) -> None:
        docs = self.sql.query(f"""SELECT doc_id, doc_name FROM {p.t('manifest')}
                                  WHERE doc_id IN ({_in(len(doc_ids))}) AND status <> 'superseded'""",
                              doc_params(doc_ids))
        judge, client = self.s.get("models.generation_endpoint"), self.llm
        # Spread the in-scope target across the bot's docs, half of them hard (EVG-4).
        n_docs = int(self.sql.query(f"SELECT count(*) AS n FROM {p.t('manifest')} "
                                    "WHERE status <> 'superseded' AND is_active")[0]["n"] or 1)
        target = self.s.get("quality.min_golden_questions", 50) * GOLDEN_SURPLUS - OUT_OF_SCOPE_COUNT
        per_doc = max(2, -(-int(target) // n_docs))
        sections = self._doc_sections(p, [d["doc_id"] for d in docs]) if docs else {}

        def write(d: dict) -> dict:   # a model call only; a document that fails doesn't stop the others
            try:
                return llm.chat_json(client, judge, qa.GOLDEN_SET_PROMPT.format(
                    purpose=cfg.purpose, n_easy=per_doc - per_doc // 2, n_hard=per_doc // 2, doc_name=d["doc_name"],
                    text=self._text_with_pages(sections.get(d["doc_id"], []))[:GOLDEN_TEXT_CHARS]), max_tokens=4000)
            except Exception as e:  # noqa: BLE001
                self.log(f"Test questions not written for {d['doc_name']}: {type(e).__name__}: {str(e)[:200]}")
                return {}

        questions: list[dict] = []
        for d, out in zip(docs, self._each(docs, write)):
            mine = sections.get(d["doc_id"], [])
            for q in (out or {}).get("questions", []):
                if not isinstance(q, dict) or not q.get("question"):
                    continue
                questions.append(self._golden_row(
                    q["question"], q.get("expected_answer", ""), d["doc_id"],
                    self._section_for_quote(mine, q.get("quote", "")), "in_scope",
                    q.get("expected_pages") or [], q.get("difficulty", "easy"), q.get("question_type", "fact")))
            issues = (out or {}).get("extraction_issues") or []
            if issues:
                self._append_qa_messages(p, d["doc_id"], qa.reviewer_notes(issues))
        existing_oos = self.sql.query(
            f"SELECT count(*) AS n FROM {p.t('golden_set')} WHERE kind = 'out_of_scope'")[0]["n"]
        if int(existing_oos or 0) == 0:
            out = llm.chat_json(client, judge, qa.OUT_OF_SCOPE_PROMPT.format(
                purpose=cfg.purpose, refuse="; ".join(cfg.refuse_topics + platform_rules(
                    self.s.get("guardrails.platform_rules", {}), cfg.disabled_rules)) or "none",
                n=OUT_OF_SCOPE_COUNT))
            questions += [self._golden_row(q, "", None, None, "out_of_scope", [], "easy", "out_of_scope")
                          for q in out.get("questions", []) if isinstance(q, str) and q.strip()]
        self._insert_golden(p, questions)

    @staticmethod
    def _golden_row(question, answer, doc_id, chunk_id, kind, expected_pages, difficulty, question_type) -> dict:
        return {"id": str(uuid.uuid4()), "q": str(question), "a": str(answer or ""), "d": doc_id, "c": chunk_id,
                "pg": [int(x) for x in expected_pages if str(x).isdigit()], "k": kind,
                "df": str(difficulty), "qt": str(question_type)}

    def _insert_golden(self, p: BotPaths, questions: list[dict]) -> None:
        """All the generated questions in one statement."""
        if not questions:
            return
        source, params = json_rows(questions, {"id": "STRING", "q": "STRING", "a": "STRING", "d": "STRING",
                                               "c": "STRING", "pg": "ARRAY<INT>", "k": "STRING", "df": "STRING",
                                               "qt": "STRING"})
        self.sql.execute(
            f"""INSERT INTO {p.t('golden_set')} (eval_id, question, expected_answer, source_doc_id,
                source_chunk_id, expected_pages, kind, difficulty, question_type, origin, approved,
                active, updated_by, updated_at)
                SELECT id, q, a, d, c, pg, k, df, qt, 'generated', false, true, 'system:generator',
                       current_timestamp() FROM {source}""", params)

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
