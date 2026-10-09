"""Every file handed to an upload is accounted for: added, already there, or refused with a reason."""
from types import SimpleNamespace

from conftest import FakeSql
from factory.documents import Documents


class _Cp:
    def __init__(self):
        self.audits = []

    def audit(self, actor, bot_id, action, target="", detail=None):
        self.audits.append((action, target, detail))


def _docs(settings, sql=None, w=None):
    cp = _Cp()
    return Documents(sql or FakeSql(), settings, cp, w), cp


def test_every_file_in_a_batch_is_reported(settings):
    stored = SimpleNamespace(files=SimpleNamespace(upload=lambda *a, **k: None))
    known = [(r"SELECT content_hash, doc_name", lambda p: [
        {"content_hash": "b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9", "doc_name": "old.txt"}])]
    docs, cp = _docs(settings, FakeSql(answers=known), stored)
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


def test_a_file_that_fails_to_store_is_reported_and_the_rest_continue(settings):
    def upload(path, data, overwrite=True):
        raise OSError("volume unavailable: /Volumes/secret/path")

    docs, cp = _docs(settings, w=SimpleNamespace(files=SimpleNamespace(upload=upload)))
    report = docs.upload_many("claims_chatbot", [{"name": "a.txt", "data": b"one"},
                                                 {"name": "bad.exe", "data": b"x"}], "ana@corp.com")
    assert [r["ok"] for r in report] == [False, False]
    assert report[0]["reasons"] == ["We couldn't store this file (OSError). Please try again."]
    assert "secret" not in report[0]["reasons"][0]
    assert ("upload_failed", "a.txt") in [(a[0], a[1]) for a in cp.audits]
    assert "aren't supported" in report[1]["reasons"][0]          # the second file was still processed


def test_register_arguments_pass_through(settings):
    seen = {}
    docs, _ = _docs(settings)
    docs.register = lambda bot, name, data, actor, **kw: (seen.update(kw), SimpleNamespace(
        ok=True, reasons=[], duplicate_of=None))[1]
    docs.upload_many("b", [{"name": "a.txt", "data": b"x", "no_expiry": False, "expires_at": "2027-01-01"}], "u")
    assert seen == {"no_expiry": False, "expires_at": "2027-01-01"}


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
