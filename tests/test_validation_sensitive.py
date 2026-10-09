import io
import zipfile

from factory.sensitive import luhn_ok, scan
from factory.validation import validate_file


def _settings(settings):
    return settings.get("ingestion")


def minimal_pdf(pages=2, encrypted=False):
    body = b"%PDF-1.7\n" + b"".join(b"<< /Type /Page >>\n" for _ in range(pages))
    body += b"<< /Type /Pages /Count %d >>\n" % pages
    if encrypted:
        body += b"trailer << /Encrypt 5 0 R >>\n"
    return body + b"%%EOF\n"


def docx_bytes(pages=3):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", "<w:document/>")
        z.writestr("docProps/app.xml", f"<Properties><Pages>{pages}</Pages></Properties>")
    return buf.getvalue()


def test_valid_pdf(settings):
    r = validate_file("policy.pdf", minimal_pdf(3), _settings(settings))
    assert r.ok and r.page_count == 3 and r.parser == "ai_parse_document"


def test_fake_pdf_rejected(settings):
    r = validate_file("policy.pdf", b"hello world", _settings(settings))
    assert not r.ok and "isn't a real PDF" in r.reasons[0]


def test_encrypted_pdf_rejected(settings):
    r = validate_file("policy.pdf", minimal_pdf(encrypted=True), _settings(settings))
    assert any("password" in x for x in r.reasons)


def test_docx_pages_and_encrypted(settings):
    assert validate_file("a.docx", docx_bytes(4), _settings(settings)).page_count == 4
    enc = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 100
    r = validate_file("a.docx", enc, _settings(settings))
    assert any("password" in x for x in r.reasons)


def test_too_many_pages(settings):
    r = validate_file("big.pdf", minimal_pdf(501), _settings(settings))
    assert any("limit is 500" in x for x in r.reasons)


def test_markdown_routes_to_text_reader(settings):
    r = validate_file("README.md", b"# Title\nBody", _settings(settings))
    assert r.ok and r.parser == "text_reader"


def test_unsupported_and_tabular_disabled(settings):
    assert not validate_file("x.exe", b"MZ", _settings(settings)).ok
    assert not validate_file("x.xlsx", b"PK\x03\x04", _settings(settings)).ok  # fast follow, off


def test_duplicate_detected_by_hash(settings):
    data = b"# same content"
    first = validate_file("a.md", data, _settings(settings))
    second = validate_file("b.md", data, _settings(settings), {first.content_hash: "a.md"})
    assert not second.ok and second.duplicate_of == "a.md"


def test_scan_blocks_restricted_and_flags_personal():
    r = scan("SSN 123-45-6789, card 4111 1111 1111 1111, mail a@b.com, call 312-555-0199")
    assert r.blocked
    assert r.restricted["Social Security numbers"] == 1
    assert r.restricted["payment card numbers"] == 1
    assert r.personal["email addresses"] == 1
    assert scan("Policy 7.2 covers water damage up to $5,000.").blocked is False


def test_luhn():
    assert luhn_ok("4111111111111111") and not luhn_ok("4111111111111112")


def _real_pdf(user_password=None, owner_password=None, pages=2):
    """A real PDF from pypdf, optionally encrypted. An owner password with an empty user password
    is the "restricted but opens freely" case (for example Page Extraction: Not Allowed)."""
    import io

    from pypdf import PdfWriter

    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=200, height=200)
    if owner_password is not None or user_password is not None:
        writer.encrypt(user_password=user_password or "", owner_password=owner_password or "owner-secret",
                       algorithm="AES-256", permissions_flag=0)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def test_restricted_pdf_that_opens_without_a_password_is_accepted(settings):
    data = _real_pdf(owner_password="owner-secret")
    assert b"/Encrypt" in data                       # it is encrypted in the technical sense
    r = validate_file("policy.pdf", data, _settings(settings))
    assert r.ok and r.reasons == [] and r.page_count == 2


def test_pdf_that_needs_a_password_to_open_is_rejected(settings):
    r = validate_file("policy.pdf", _real_pdf(user_password="open-me"), _settings(settings))
    assert not r.ok and any("needs a password to open" in x for x in r.reasons)


def test_plain_pdf_is_accepted_and_counted(settings):
    r = validate_file("policy.pdf", _real_pdf(pages=3), _settings(settings))
    assert r.ok and r.page_count == 3


def test_unreadable_pdf_with_an_encryption_marker_is_still_rejected(settings):
    """When the file can't be opened to check, the marker alone is enough to refuse it."""
    r = validate_file("policy.pdf", minimal_pdf(encrypted=True), _settings(settings))
    assert not r.ok and any("couldn't check" in x and "password" in x for x in r.reasons)
