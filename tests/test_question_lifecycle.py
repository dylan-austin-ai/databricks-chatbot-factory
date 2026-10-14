"""Test questions follow their documents: flagged when a document changes, checked before scoring."""
import pathlib

import pytest

from conftest import FakeSql
from factory import evals, golden, problems, qa
from factory.documents import Documents
from factory.ingestion import BotPaths

ROOT = pathlib.Path(__file__).resolve().parents[1]
QUESTIONS = r"SELECT eval_id FROM `chatbots_test`\.`claims_chatbot`\.`golden_set`"


class Cp:
    def __init__(self):
        self.audits = []

    def audit(self, actor, bot_id, action, target="", detail=None):
        self.audits.append((action, target, detail))

    def audit_many(self, actor, bot_id, action, items):
        self.audits += [(action, target, detail) for target, detail in items]


def docs(settings, relying=("e1", "e2")):
    golden._REVIEW_READY.clear()
    sql = FakeSql(answers=[(QUESTIONS, [{"eval_id": e} for e in relying]),
                           (r"DESCRIBE TABLE", [{"col_name": "eval_id"}, {"col_name": "review_note"}])])
    cp = Cp()
    return Documents(sql, settings, cp), sql, cp


def review_updates(sql):
    return [(s, p) for s, p in sql.statements if s.startswith("UPDATE") and "golden_set" in s]


@pytest.mark.parametrize("act, why", [
    (lambda d: d.archive("claims_chatbot", "doc1", "ana@corp.com"), "archived"),
    (lambda d: d.flag("claims_chatbot", "doc1", "ana@corp.com", "wrong table"), "flagged"),
    (lambda d: d.change_many("claims_chatbot", ["doc1"], "archive", "ana@corp.com"), "archived"),
    (lambda d: d.change_many("claims_chatbot", ["doc1"], "delete", "admin@corp.com", is_admin=True, reason="dupe"),
     "deleted"),
    (lambda d: d.hard_delete("claims_chatbot", "doc1", "admin@corp.com", True, "legal hold"), "deleted"),
    (lambda d: d.set_page_range("claims_chatbot", "doc1", "1-3", "ana@corp.com"), "edited"),
])
def test_when_a_document_changes_the_questions_relying_on_it_lose_their_approval_with_the_reason(settings, act, why):
    d, sql, cp = docs(settings)
    act(d)
    (update, params), = review_updates(sql)
    assert "SET approved = false, review_note = :w" in update and "active AND approved AND source_doc_id IN" in update
    assert params["d0"] == "doc1" and params["w"].startswith(f"Needs review: {golden.REVIEW_REASONS[why]} on ")
    assert "set its Source document, then approve it again" in params["w"]
    audit = [a for a in cp.audits if a[0] == "questions_need_review"]
    assert audit == [("questions_need_review", "", {"doc_ids": ["doc1"], "eval_ids": ["e1", "e2"], "reason": why})]


def test_re_reading_the_same_pages_leaves_the_questions_alone(settings):
    d, sql, cp = docs(settings)
    sql.answers.insert(0, (r"SELECT page_range FROM", [{"page_range": "1-3"}]))
    d.set_page_range("claims_chatbot", "doc1", "1 - 3".replace(" ", ""), "ana@corp.com")
    assert review_updates(sql) == []
    d.set_page_range("claims_chatbot", "doc1", "1-4", "ana@corp.com")
    assert len(review_updates(sql)) == 1


def test_a_new_version_flags_the_questions_on_the_old_one(settings):
    d, sql, cp = docs(settings)
    d._write_manifest(BotPaths(settings, "claims_chatbot"), [], ["0123456789abcdef_v2"], "ana@corp.com")
    (update, params), = review_updates(sql)
    assert params["d0"] == "0123456789abcdef" and "replaced by a new version" in params["w"]


def test_nothing_is_written_when_no_approved_question_relies_on_the_document(settings):
    d, sql, cp = docs(settings, relying=())
    d.archive("claims_chatbot", "doc1", "ana@corp.com")
    assert review_updates(sql) == [] and not [a for a in cp.audits if a[0] == "questions_need_review"]


def test_older_tables_get_the_review_column_once(settings):
    golden._REVIEW_READY.clear()
    sql = FakeSql(answers=[(r"DESCRIBE TABLE", [{"col_name": "eval_id"}])])
    p = BotPaths(settings, "claims_chatbot")
    golden.ensure_review_note(sql, p)
    golden.ensure_review_note(sql, p)
    assert [s for s, _ in sql.statements if s.startswith("ALTER")] == [
        "ALTER TABLE `chatbots_test`.`claims_chatbot`.`golden_set` ADD COLUMNS (review_note STRING)"]
    ddl = (ROOT / "sql" / "bot_schema_ddl.sql").read_text()
    assert "review_note STRING" in ddl[ddl.index("golden_set ("):ddl.index("access_probe")]


def test_the_check_stops_before_scoring_when_a_question_points_at_a_document_not_in_use():
    cases = [{"eval_id": "e1", "question": "How do I add my spouse in myCigna?", "source_doc_id": "gone"},
             {"eval_id": "e2", "question": "Hip dislocation certificate?", "source_doc_id": "old"},
             {"eval_id": "e3", "question": "Coaching hours?", "source_doc_id": "ok"},
             {"eval_id": "e4", "question": "Write me a poem", "source_doc_id": None}]
    stale = evals.stale_sources(cases, {"old": "archived", "ok": "approved"})
    assert [(c["eval_id"], why) for c, why in stale] == [
        ("e1", "its source document no longer exists"), ("e2", "its source document is archived")]
    message = evals.stale_message(stale, "hr_bot")
    assert message.startswith("2 approved test question(s) for hr_bot point at a document that is missing or not in use, "
                              "so the check was stopped before anything was asked or scored.")
    assert "e1 “How do I add my spouse in myCigna?” (its source document no longer exists)" in message
    kind, advice = problems.job_stopped(message)
    assert kind == problems.SETUP and "Test questions" in advice and "Source document" in advice
    job = (ROOT / "jobs" / "run_evals.py").read_text()
    assert job.index("stale = evals.stale_sources(cases, in_use)") < job.index("mlflow.genai.evaluate(data=records")


def test_owners_choose_a_current_source_document_and_approving_clears_the_note(settings):
    labels = golden.source_labels([
        {"doc_id": "a1b2c3d4e5", "doc_name": "Certificate.pdf", "status": "approved"},
        {"doc_id": "f6e5d4c3b2", "doc_name": "Certificate.pdf", "status": "archived"},
        {"doc_id": "x1", "doc_name": "Coaching.pdf", "status": "pending_review"}])
    assert labels == {"a1b2c3d4e5": "Certificate.pdf [a1b2c3]", "f6e5d4c3b2": "Certificate.pdf [f6e5d4] (archived)",
                      "x1": "Coaching.pdf (pending review)"}
    loaded = [{"eval_id": "e2", "question": "q", "expected_answer": "a", "kind": "in_scope", "approved": False,
               "source_doc_id": "f6e5d4c3b2"}]
    delta = golden.changes(loaded, [{**loaded[0], "approved": True, "source_doc_id": "a1b2c3d4e5", "kind": "Should answer"}])
    assert delta["update"][0]["source_doc_id"] == "a1b2c3d4e5"
    (merge, params), = golden.save_statements(BotPaths(settings, "claims_chatbot"), delta, "ana@corp.com")
    assert "source_doc_id = s.source_doc_id" in merge
    assert "review_note = CASE WHEN s.approved THEN NULL ELSE t.review_note END" in merge
    page = (ROOT / "app" / "pages" / "test_questions.py").read_text()
    assert '"Source document", options=' in page and '"Needs review because", disabled=True' in page
    assert '"changed": [r["eval_id"] for r in change["update"]]' in page      # which questions, in the audit log


def test_generated_questions_stand_on_their_own_and_ask_about_current_policy():
    assert 'name the system, program, form or benefit it is about (for example "in myCigna" or "in Workday")' in \
        qa.GOLDEN_SET_PROMPT
    assert "not about a worked example, a sample value in a screenshot or a past deadline" in qa.GOLDEN_SET_PROMPT
