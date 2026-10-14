"""Saving the test-question table (EVL-1..4).

The page shows every question in an editable table. Saving used to write every row back, one
statement each, whether or not it changed. Here the edited table is compared with what was
loaded, and only the differences are written: at most one statement each for new questions,
changed questions and removed questions.
"""
from __future__ import annotations

import uuid
from datetime import date

from .ingestion import BotPaths
from .sql import in_list, json_rows

KIND_LABEL = {"in_scope": "Should answer", "out_of_scope": "Should refuse",
              "rule_bound": "Share documents, don't decide", "feedback": "From user feedback"}
_KIND = {label: kind for kind, label in KIND_LABEL.items()}
FIELDS = ("question", "expected_answer", "kind", "approved", "source_doc_id")


def _text(value) -> str:
    return "" if value is None or value != value else str(value)   # NaN from an empty table cell


def _ticked(value) -> bool:
    return value is not None and value == value and bool(value)   # an empty checkbox cell is not a tick


def _clean(row: dict) -> dict:
    kind = _text(row.get("kind"))
    return {"question": _text(row.get("question")).strip(), "expected_answer": _text(row.get("expected_answer")).strip(),
            "kind": _KIND.get(kind, kind if kind in KIND_LABEL else "in_scope"), "approved": _ticked(row.get("approved")),
            "source_doc_id": _text(row.get("source_doc_id")).strip() or None}


def changes(original: list[dict], edited: list[dict]) -> dict[str, list]:
    """What saving has to do. `original` is what was loaded, `edited` what is in the table now;
    rows carry eval_id, question, expected_answer, kind (code or label), approved and optionally
    delete. Returns {"add": [...], "update": [...], "remove": [eval_id, ...]}.

    A row with no eval_id and a question is new. A row ticked delete, or one that was loaded and
    is no longer in the table, is removed. A loaded row is updated only if a field differs."""
    before = {str(r["eval_id"]): _clean(r) for r in original if _text(r.get("eval_id"))}
    add, update, remove, seen = [], [], [], set()
    for row in edited:
        eval_id, now = _text(row.get("eval_id")), _clean(row)
        if not eval_id:
            if now["question"] and not _ticked(row.get("delete")):
                add.append(now)
            continue
        seen.add(eval_id)
        if eval_id not in before:
            continue   # not something this page loaded; leave it alone
        if _ticked(row.get("delete")):
            remove.append(eval_id)
        elif any(now[f] != before[eval_id][f] for f in FIELDS) and now["question"]:
            update.append({"eval_id": eval_id, **now})
    remove += [eval_id for eval_id in before if eval_id not in seen]
    return {"add": add, "update": update, "remove": remove}


def counts(delta: dict[str, list]) -> str:
    """"2 added, 3 changed, 1 removed" for the message after saving."""
    parts = [f"{len(delta[key])} {word}" for key, word in (("add", "added"), ("update", "changed"), ("remove", "removed"))
             if delta[key]]
    return ", ".join(parts) or "nothing to save"


def save_statements(p: BotPaths, delta: dict[str, list], actor: str) -> list[tuple[str, dict]]:
    """The statements that apply `delta`: at most three, however many rows changed."""
    out: list[tuple[str, dict]] = []
    table = p.t("golden_set")
    row_type = {"eval_id": "STRING", "question": "STRING", "expected_answer": "STRING", "kind": "STRING",
                "approved": "BOOLEAN", "source_doc_id": "STRING"}
    if delta["add"]:
        source, params = json_rows([{"eval_id": str(uuid.uuid4()), **r} for r in delta["add"]], row_type)
        out.append((f"""INSERT INTO {table} (eval_id, question, expected_answer, kind, difficulty, question_type,
                        origin, approved, active, updated_by, updated_at, source_doc_id)
                        SELECT eval_id, question, expected_answer, kind, 'hard', 'owner', 'manual', approved, true,
                               :u, current_timestamp(), source_doc_id FROM {source}""", {**params, "u": actor}))
    if delta["update"]:
        # Approving a question again clears why it needed review; the table's history keeps it.
        source, params = json_rows(delta["update"], row_type)
        out.append((f"""MERGE INTO {table} t USING {source} s ON t.eval_id = s.eval_id
                        WHEN MATCHED THEN UPDATE SET question = s.question, expected_answer = s.expected_answer,
                          kind = s.kind, approved = s.approved, source_doc_id = s.source_doc_id,
                          review_note = CASE WHEN s.approved THEN NULL ELSE t.review_note END,
                          updated_by = :u, updated_at = current_timestamp()""",
                    {**params, "u": actor}))
    if delta["remove"]:
        chosen, params = in_list(delta["remove"], "e")
        out.append((f"UPDATE {table} SET active = false, updated_by = :u, updated_at = current_timestamp() "
                    f"WHERE eval_id IN {chosen}", {**params, "u": actor}))
    return out


# When a question's document changes ------------------------------------------------------------------
_REVIEW_READY: set[str] = set()
REVIEW_REASONS = {"archived": "its source document was archived", "deleted": "its source document was deleted",
                  "replaced": "its source document was replaced by a new version",
                  "flagged": "its source document was flagged as needing fixes",
                  "edited": "its source document's text or pages were changed by hand"}


def ensure_review_note(sql, p: BotPaths) -> None:
    """Tables made before review notes lack the column; add it, once per process."""
    table = p.t("golden_set")
    if table in _REVIEW_READY:
        return
    try:
        if "review_note" not in {str(r.get("col_name", "")).lower() for r in sql.query(f"DESCRIBE TABLE {table}")}:
            sql.execute(f"ALTER TABLE {table} ADD COLUMNS (review_note STRING)")
    except Exception:  # noqa: BLE001 - a table that isn't there yet is created with the column
        return
    _REVIEW_READY.add(table)


def flag_for_review(sql, p: BotPaths, doc_ids, why: str) -> list[str]:
    """Take approval away from the approved questions whose source document just changed, and
    say why. The question itself is left as it was (the table's history keeps every version);
    the owner checks it against a current document and approves it again. Returns their eval_ids."""
    doc_ids = [d for d in dict.fromkeys(doc_ids) if d]
    if not doc_ids:
        return []
    ensure_review_note(sql, p)
    listed, params = in_list(doc_ids, "d")
    table = p.t("golden_set")
    where = f"active AND approved AND source_doc_id IN {listed}"
    found = [r["eval_id"] for r in sql.query(f"SELECT eval_id FROM {table} WHERE {where}", params)]
    if found:
        note = (f"Needs review: {REVIEW_REASONS.get(why, why)} on {date.today().isoformat()}. Check the expected answer "
                "against a current document, set its Source document, then approve it again.")
        sql.execute(f"UPDATE {table} SET approved = false, review_note = :w, updated_by = 'system:documents', "
                    f"updated_at = current_timestamp() WHERE {where}", {**params, "w": note})
    return found


def source_labels(docs: list[dict]) -> dict[str, str]:
    """doc_id -> what the Source document column shows: the name of a document in use, or the name
    with what is wrong with it. Two documents with the same name are told apart by their id."""
    names = [d["doc_name"] for d in docs]
    out = {}
    for d in docs:
        name = d["doc_name"] + (f" [{d['doc_id'][:6]}]" if names.count(d["doc_name"]) > 1 else "")
        out[d["doc_id"]] = name if d["status"] == "approved" else f"{name} ({d['status'].replace('_', ' ')})"
    return out
