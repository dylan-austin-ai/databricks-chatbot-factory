"""Every file handed to an upload is accounted for: added, already there, or refused with a reason."""
from types import SimpleNamespace

from conftest import FakeSql
from factory.documents import Documents


class _Cp:
    def __init__(self):
        self.audits = []

    def audit(self, actor, bot_id, action, target="", detail=None):
        self.audits.append((action, target, detail))

    def audit_many(self, actor, bot_id, action, items):
        self.audits += [(action, target, detail) for target, detail in items]


def _docs(settings, sql=None, w=None):
    cp = _Cp()
    return Documents(sql or FakeSql(), settings, cp, w), cp


KNOWN = r"SELECT doc_id, doc_version, doc_name, content_hash, status, no_expiry, doc_owner, "
HELLO = "b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9"   # sha256("hello world")


def _rows(sql):
    """The rows a batch INSERT carried, decoded from its single JSON parameter."""
    import json

    stmt, params = [(s, p) for s, p in sql.statements if s.lstrip().startswith("INSERT INTO") and "`manifest`" in s][0]
    return stmt, json.loads(params["rows"])


def test_every_file_in_a_batch_is_reported(settings):
    stored = []
    w = SimpleNamespace(files=SimpleNamespace(upload=lambda path, data, overwrite=True: stored.append(path)))
    sql = FakeSql(answers=[(KNOWN, [{"doc_id": "old1", "doc_version": "1", "doc_name": "old.txt", "content_hash": HELLO,
                                     "status": "approved", "no_expiry": "true", "doc_owner": "raj@corp.com"}])])
    docs, cp = _docs(settings, sql, w)
    report = docs.upload_many("claims_chatbot", [
        {"name": "notes.txt", "data": b"fresh content"},
        {"name": "copy.txt", "data": b"hello world"},           # same bytes as old.txt
        {"name": "virus.exe", "data": b"MZ..."},
        {"name": "empty.txt", "data": b""},
    ], "ana@corp.com")
    by_name = {r["name"]: r for r in report}
    assert [r["name"] for r in report] == ["notes.txt", "copy.txt", "virus.exe", "empty.txt"]
    assert by_name["notes.txt"] == {"name": "notes.txt", "ok": True, "reasons": [], "duplicate": False}
    assert not by_name["copy.txt"]["ok"] and by_name["copy.txt"]["duplicate"]
    assert not by_name["virus.exe"]["ok"] and "aren't supported" in by_name["virus.exe"]["reasons"][0]
    assert by_name["empty.txt"]["reasons"] == ["This file is empty."]
    assert [a[1] for a in cp.audits if a[0] == "upload_rejected"] == ["copy.txt", "virus.exe", "empty.txt"]
    stmt, rows = _rows(sql)
    assert len(rows) == 1 and rows[0]["doc_name"] == "notes.txt" and rows[0]["doc_version"] == 1
    assert rows[0]["status"] == "pending_review" and rows[0]["doc_owner"] == "ana@corp.com" and len(stored) == 1
    assert "FAILFAST" in stmt and "CAST(expires_at AS TIMESTAMP)" in stmt


def test_a_batch_costs_the_same_number_of_statements_as_one_file(settings):
    w = SimpleNamespace(files=SimpleNamespace(upload=lambda *a, **k: None))
    one, many = FakeSql(), FakeSql()
    _docs(settings, one, w)[0].upload_many("claims_chatbot", [{"name": "a.txt", "data": b"a"}], "ana@corp.com")
    _docs(settings, many, w)[0].upload_many(
        "claims_chatbot", [{"name": f"f{i}.txt", "data": f"text {i}".encode()} for i in range(20)], "ana@corp.com")
    assert len(many.statements) == len(one.statements) == 2          # one read, one insert
    assert len(_rows(many)[1]) == 20


def test_same_name_becomes_a_new_version_and_replaces_the_old_one(settings):
    w = SimpleNamespace(files=SimpleNamespace(upload=lambda *a, **k: None))
    sql = FakeSql(answers=[(KNOWN, [
        {"doc_id": "d1", "doc_version": "1", "doc_name": "policy.txt", "content_hash": "x", "status": "superseded",
         "no_expiry": "true", "doc_owner": "raj@corp.com"},
        {"doc_id": "d1", "doc_version": "2", "doc_name": "policy.txt", "content_hash": "y", "status": "approved",
         "no_expiry": "false", "doc_owner": "raj@corp.com", "expires_at": "2099-06-30 00:00:00"}])])
    docs, cp = _docs(settings, sql, w)
    docs.upload_many("claims_chatbot", [{"name": "policy.txt", "data": b"version three"},
                                        {"name": "policy.txt", "data": b"version four"}], "ana@corp.com")
    import json

    replaced = [json.loads(p["rows"]) for s, p in sql.statements if s.startswith("UPDATE")][0]
    assert replaced == [{"k": "d1_v2"}]                                   # the stored version it replaces
    _, rows = _rows(sql)
    assert [(r["doc_id"], r["doc_version"], r["status"]) for r in rows] == [("d1", 3, "superseded"), ("d1", 4, "pending_review")]
    assert all(r["doc_owner"] == "raj@corp.com" and r["no_expiry"] is False
               and r["expires_at"] == "2099-06-30 00:00:00" for r in rows)       # owner and expiry date inherited
    assert [a[2]["version"] for a in cp.audits if a[0] == "doc_uploaded"] == [3, 4]


def test_a_file_that_fails_to_store_is_reported_and_the_rest_continue(settings):
    def upload(path, data, overwrite=True):
        if "_v1.txt" in path and upload.calls == 0:
            upload.calls += 1
            raise OSError("volume unavailable: /Volumes/secret/path")
    upload.calls = 0

    sql = FakeSql()
    docs, cp = _docs(settings, sql, w=SimpleNamespace(files=SimpleNamespace(upload=upload)))
    report = docs.upload_many("claims_chatbot", [{"name": "a.txt", "data": b"one"}, {"name": "b.txt", "data": b"two"},
                                                 {"name": "bad.exe", "data": b"x"}], "ana@corp.com")
    assert [r["ok"] for r in report] == [False, True, False]
    assert report[0]["reasons"] == ["We couldn't store this file (OSError). Please try again."]
    assert "secret" not in report[0]["reasons"][0]
    assert ("upload_failed", "a.txt") in [(a[0], a[1]) for a in cp.audits]
    assert [r["doc_name"] for r in _rows(sql)[1]] == ["b.txt"]            # the second file still went in


def test_if_the_table_write_fails_no_file_is_reported_as_added(settings):
    class Broken(FakeSql):
        def execute(self, statement, params=None):
            raise RuntimeError("warehouse stopped")

    docs, cp = _docs(settings, Broken(), w=SimpleNamespace(files=SimpleNamespace(upload=lambda *a, **k: None)))
    report = docs.upload_many("claims_chatbot", [{"name": "a.txt", "data": b"one"}], "ana@corp.com")
    assert report == [{"name": "a.txt", "ok": False, "duplicate": False,
                       "reasons": ["We couldn't store this file (RuntimeError). Please try again."]}]
    assert [a[0] for a in cp.audits] == ["upload_failed"]


def test_upload_options_reach_the_table(settings):
    sql = FakeSql()
    docs, _ = _docs(settings, sql)
    docs.upload_many("b", [{"name": "a.txt", "data": b"x", "no_expiry": False, "expires_at": "2027-01-01",
                            "doc_owner": "lee@corp.com"}], "u")
    row = _rows(sql)[1][0]
    assert (row["no_expiry"], row["expires_at"], row["doc_owner"]) == (False, "2027-01-01", "lee@corp.com")


def test_not_uploaded_lists_refusals_that_are_still_missing(settings):
    rows = [
        {"ts": "2026-10-09 10:05", "actor": "ana@corp.com", "file_name": "locked.pdf",
         "detail_json": '{"reasons": ["This PDF needs a password to open."]}'},
        {"ts": "2026-10-09 09:00", "actor": "ana@corp.com", "file_name": "locked.pdf",
         "detail_json": '{"reasons": ["older attempt"]}'},
        {"ts": "2026-10-09 08:00", "actor": "raj@corp.com", "file_name": "copy.txt",
         "detail_json": '{"reasons": ["This is an exact duplicate of \'old.txt\', already uploaded."]}'},
    ]
    sql = FakeSql(answers=[(r"FROM chatbots_test\._platform\.audit_log", rows)])
    docs, _ = _docs(settings, sql)
    missing = docs.not_uploaded("claims_chatbot", days=30)
    assert missing == [{"file_name": "locked.pdf", "when": "2026-10-09 10:05", "by": "ana@corp.com",
                        "reasons": ["This PDF needs a password to open."]}]
    query = sql.find(r"audit_log")[0]
    assert "'upload_rejected', 'upload_failed'" in query and "INTERVAL 30 DAYS" in query
    assert "NOT EXISTS" in query and "m.uploaded_at > a.ts" in query     # re-uploaded since: no longer listed


def test_approve_all_waiting_is_three_statements_and_skips_blocked_documents(settings):
    import json

    sql = FakeSql(answers=[(r"SELECT doc_id FROM .*status = 'pending_review'", [{"doc_id": "a"}, {"doc_id": "b"}])])
    docs, cp = _docs(settings, sql)
    assert docs.bulk_approve_pending("claims_chatbot", "ana@corp.com") == 2
    select = sql.find(r"SELECT doc_id FROM")[0]
    assert "status = 'pending_review'" in select and "Contains sensitive info" in select
    assert "readability" not in select                      # every badge, unlike the Good-only button
    update, params = [(s, p) for s, p in sql.statements if s.startswith("UPDATE")][0]
    assert "Contains sensitive info" in update and json.loads(params["rows"]) == [{"d": "a"}, {"d": "b"}]
    assert [a[1] for a in cp.audits if a[0] == "doc_approved"] == ["a", "b"]
    assert len(sql.statements) == 2                          # the audit rows go through audit_many

    good = FakeSql(answers=[(r"SELECT doc_id FROM", [{"doc_id": "a"}])])
    assert _docs(settings, good)[0].bulk_approve_green("claims_chatbot", "ana@corp.com") == 1
    assert "readability IN ('green', 'n/a')" in good.find(r"SELECT doc_id FROM")[0]
    assert _docs(settings, FakeSql())[0].bulk_approve_pending("claims_chatbot", "ana@corp.com") == 0


def test_hand_typed_text_can_be_read_back_and_removed(settings):
    saved = [{"text": "1. Click Benefits\n2. Choose Enroll", "saved_at": "2026-10-10 09:30:00"}]
    sql = FakeSql(answers=[(r"pe\.is_override", saved), (r"SELECT doc_version FROM", [{"doc_version": 2}])])
    docs, cp = _docs(settings, sql)
    assert docs.current_override("claims_chatbot", "d1") == saved[0]
    docs.remove_override("claims_chatbot", "d1", "ana@corp.com")
    delete, params = [(s, p) for s, p in sql.statements if s.startswith("DELETE FROM")][0]
    assert "is_override" in delete and "doc_version = CAST(:v AS INT)" in delete and params == {"d": "d1", "v": 2}
    assert ("doc_override_removed", "d1") in [(a[0], a[1]) for a in cp.audits]
    empty, _ = _docs(settings, FakeSql())
    assert empty.current_override("claims_chatbot", "d1") is None


def test_a_new_version_keeps_an_expiry_date_only_while_it_is_ahead():
    from factory.documents import inherited_expiry

    future = {"no_expiry": "false", "expires_at": "2099-06-30T00:00:00.000Z"}
    past = {"no_expiry": False, "expires_at": "2001-01-01 00:00:00"}
    assert inherited_expiry(future, True, None) == (False, "2099-06-30T00:00:00.000Z")
    assert inherited_expiry(past, True, None) == (True, None)              # an expired date isn't carried over
    assert inherited_expiry({"no_expiry": "true", "expires_at": None}, True, None) == (True, None)
    assert inherited_expiry(future, False, "2027-01-01") == (False, "2027-01-01")   # the uploader chose
    assert inherited_expiry(None, True, None) == (True, None)
