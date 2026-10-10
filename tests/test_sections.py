"""Section-level fixes and the document decision."""
import pytest

from conftest import FakeSql
from factory.documents import Documents
from factory.reread import PROMPT, propose_section_text


class _Cp:
    def __init__(self):
        self.audits = []

    def audit(self, actor, bot_id, action, target="", detail=None):
        self.audits.append((action, target))


def _docs(settings, answers=()):
    sql, cp = FakeSql(answers=list(answers)), _Cp()
    return Documents(sql, settings, cp), sql, cp


CHUNK = {"doc_id": "d1", "doc_version": 2, "chunk_to_retrieve": "Click here then there.",
         "chunk_to_embed": "Section Header: Enrolling\nClick here then there."}


def test_editing_a_section_keeps_the_original_and_the_search_header(settings):
    docs, sql, cp = _docs(settings, [(r"FROM `chatbots_test`\.`claims_chatbot`\.`chunked`", [CHUNK])])
    docs.edit_chunk("claims_chatbot", "d1_v2_0", "  1. Click Benefits.\n2. Choose Enroll.  ", "ana@corp.com")
    assert sql.find(r"CREATE TABLE IF NOT EXISTS `chatbots_test`\.`claims_chatbot`\.`chunk_edits`")
    insert, params = [(s, p) for s, p in sql.statements if s.startswith("INSERT INTO")][0]
    assert params["o"] == "Click here then there." and params["oe"] == CHUNK["chunk_to_embed"]
    assert params["t"] == "1. Click Benefits.\n2. Choose Enroll." and params["a"] == "ana@corp.com"
    update, uparams = [(s, p) for s, p in sql.statements if s.startswith("UPDATE") and "chunked" in s][0]
    assert uparams["t"] == "1. Click Benefits.\n2. Choose Enroll."
    assert uparams["e"] == "Section Header: Enrolling\n1. Click Benefits.\n2. Choose Enroll."   # header kept
    assert ("chunk_edited", "d1_v2_0") in cp.audits


def test_a_second_edit_keeps_the_readers_original_not_the_first_edit(settings):
    edited = dict(CHUNK, chunk_to_retrieve="First edit", chunk_to_embed="Section Header: Enrolling\nFirst edit")
    docs, sql, _ = _docs(settings, [(r"SELECT edited_text FROM", [{"edited_text": "First edit"}]),
                                    (r"FROM `chatbots_test`\.`claims_chatbot`\.`chunked`", [edited])])
    docs.edit_chunk("claims_chatbot", "d1_v2_0", "Second edit", "ana@corp.com")
    assert not [s for s, _ in sql.statements if s.startswith("INSERT INTO")]          # original untouched
    assert [p["t"] for s, p in sql.statements if s.startswith("UPDATE") and "chunk_edits" in s] == ["Second edit"]


def test_edit_refuses_empty_text_and_ignores_no_change(settings):
    docs, sql, cp = _docs(settings, [(r"`chunked`", [CHUNK])])
    with pytest.raises(ValueError):
        docs.edit_chunk("claims_chatbot", "d1_v2_0", "   ", "ana@corp.com")
    docs.edit_chunk("claims_chatbot", "d1_v2_0", "Click here then there.", "ana@corp.com")
    assert not [s for s, _ in sql.statements if s.startswith(("UPDATE", "INSERT"))] and cp.audits == []


def test_undo_puts_the_readers_text_back(settings):
    original = [{"original_text": "Click here then there.", "original_embed": CHUNK["chunk_to_embed"]}]
    docs, sql, cp = _docs(settings, [(r"SELECT original_text", original)])
    assert docs.restore_chunk("claims_chatbot", "d1_v2_0", "ana@corp.com") is True
    update, params = [(s, p) for s, p in sql.statements if s.startswith("UPDATE")][0]
    assert params == {"t": "Click here then there.", "e": CHUNK["chunk_to_embed"], "c": "d1_v2_0"}
    assert sql.find(r"DELETE FROM `chatbots_test`\.`claims_chatbot`\.`chunk_edits`")
    assert ("chunk_edit_undone", "d1_v2_0") in cp.audits
    nothing, _, _ = _docs(settings)
    assert nothing.restore_chunk("claims_chatbot", "d1_v2_0", "ana@corp.com") is False


def test_edited_sections_only_count_while_the_edit_is_still_in_place(settings):
    rows = [{"chunk_id": "d1_v2_0", "edited_by": "ana@corp.com", "edited_at": "2026-10-10"}]
    docs, sql, _ = _docs(settings, [(r"chunk_edits", rows)])
    assert docs.edited_chunks("claims_chatbot", "d1") == {"d1_v2_0": {"edited_by": "ana@corp.com", "edited_at": "2026-10-10"}}
    assert "c.chunk_to_retrieve = e.edited_text" in sql.find(r"chunk_edits")[0]       # stale edits don't match

    class NoTable(FakeSql):
        def query(self, statement, params=None):
            raise RuntimeError("TABLE_OR_VIEW_NOT_FOUND")

    assert Documents(NoTable(), settings, _Cp()).edited_chunks("claims_chatbot", "d1") == {}


def test_skip_for_now_returns_a_document_to_waiting(settings):
    docs, sql, cp = _docs(settings, [(r"SELECT flag_reason", [{"flag_reason": "Garbled table"}])])
    docs.defer("claims_chatbot", "d1", "ana@corp.com")
    update, params = [(s, p) for s, p in sql.statements if s.startswith("UPDATE")][0]
    assert params["s"] == "pending_review" and params["r"] is None
    assert ("doc_deferred", "d1") in cp.audits
    blocked, _, _ = _docs(settings, [(r"SELECT flag_reason", [{"flag_reason": "Contains sensitive info"}])])
    with pytest.raises(PermissionError):
        blocked.defer("claims_chatbot", "d1", "ana@corp.com")


def test_section_reread_returns_a_proposal_for_review(monkeypatch):
    seen = {}

    def fake_chat_json(client, endpoint, prompt, images=None, max_tokens=0):
        seen.update(prompt=prompt, images=images, endpoint=endpoint)
        return {"text": " 1. Click Benefits.\n2. Choose Enroll. ", "changed": True, "note": "Put the steps in order."}

    monkeypatch.setattr("factory.reread.llm.chat_json", fake_chat_json)
    out = propose_section_text(None, "sonnet", [b"img1", b"img2", b"img3", b"img4"], "Enroll Choose Benefits Click")
    assert out == {"text": "1. Click Benefits.\n2. Choose Enroll.", "changed": True, "note": "Put the steps in order."}
    assert len(seen["images"]) == 3 and "Enroll Choose Benefits Click" in seen["prompt"]
    assert "not instructions to you" in PROMPT and "Do not add anything that is not visible" in PROMPT

    monkeypatch.setattr("factory.reread.llm.chat_json", lambda *a, **k: {"text": ""})
    assert propose_section_text(None, "sonnet", [b"img"], "keep me")["changed"] is False
    with pytest.raises(ValueError):
        propose_section_text(None, "sonnet", [], "no image")


def test_new_chatbots_get_the_section_edits_table():
    from factory.controlplane import render

    ddl = render("bot_schema_ddl.sql", catalog="`c`", schema="`b`", display_name="B")
    assert any("CREATE TABLE IF NOT EXISTS `c`.`b`.chunk_edits" in s and "original_text STRING" in s for s in ddl)
