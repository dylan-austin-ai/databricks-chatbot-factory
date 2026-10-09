"""Upload validation (ING-1, ING-2).

Checks actual file contents, not just the extension: magic bytes, size, page
count, password protection, corruption, and duplicate content (sha256).
Every rejection carries a plain-English reason for the creator.
"""
from __future__ import annotations

import hashlib
import io
import re
import zipfile
from dataclasses import dataclass, field

OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"   # legacy .doc/.ppt/.xls, and encrypted OOXML
ZIP_MAGIC = b"PK\x03\x04"
PDF_MAGIC = b"%PDF"
IMAGE_MAGIC = {
    "png": [b"\x89PNG\r\n\x1a\n"],
    "jpg": [b"\xff\xd8\xff"], "jpeg": [b"\xff\xd8\xff"],
    "tif": [b"II*\x00", b"MM\x00*"], "tiff": [b"II*\x00", b"MM\x00*"],
}
OOXML_MAIN_PART = {"docx": "word/document.xml", "pptx": "ppt/presentation.xml",
                   "xlsx": "xl/workbook.xml"}


@dataclass
class ValidationResult:
    ok: bool
    file_name: str
    ext: str
    content_hash: str
    size_bytes: int
    page_count: int | None = None
    parser: str | None = None        # ai_parse_document | text_reader | tabular
    reasons: list[str] = field(default_factory=list)
    duplicate_of: str | None = None  # doc_name of an existing doc with the same content


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _ext(name: str) -> str:
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def _pdf_checks(data: bytes) -> tuple[int | None, list[str]]:
    reasons = []
    # A PDF can be encrypted and still open for anyone: an owner password that only restricts
    # printing, copying or page extraction leaves the open password empty. Those are accepted.
    # Only a file that really needs a password to open is refused, so open it to find out.
    pages, needs_password = None, None
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        needs_password = bool(reader.is_encrypted) and not reader.decrypt("")
        if not needs_password:
            pages = len(reader.pages)
    except Exception:  # noqa: BLE001 - unreadable here; fall back to the cheap checks below
        pass
    if needs_password:
        reasons.append("This PDF needs a password to open. Please upload a copy that opens without one.")
    elif needs_password is None and (b"/Encrypt" in data[-65536:] or b"/Encrypt" in data[:2048]):
        reasons.append("This PDF is encrypted and we couldn't check whether it opens without a password. "
                       "Please upload an unprotected copy.")
    if b"%%EOF" not in data[-2048:]:
        reasons.append("This PDF looks damaged or incomplete. Please re-export it and try again.")
    if pages is None:
        pages = len(re.findall(rb"/Type\s*/Page(?!s)", data)) or None
    return pages, reasons


def _ooxml_checks(data: bytes, ext: str) -> tuple[int | None, list[str]]:
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
        bad = z.testzip()
    except zipfile.BadZipFile:
        return None, ["This file looks damaged. Please re-save it and upload again."]
    if bad:
        return None, ["This file looks damaged. Please re-save it and upload again."]
    names = set(z.namelist())
    if OOXML_MAIN_PART[ext] not in names:
        return None, [f"This doesn't look like a real .{ext} file."]
    pages = None
    if ext == "pptx":
        pages = sum(1 for n in names if re.match(r"ppt/slides/slide\d+\.xml$", n))
    elif ext == "docx" and "docProps/app.xml" in names:
        m = re.search(rb"<Pages>(\d+)</Pages>", z.read("docProps/app.xml"))
        pages = int(m.group(1)) if m else None
    return pages, []


def validate_file(file_name: str, data: bytes, settings: dict,
                  existing_hashes: dict[str, str] | None = None) -> ValidationResult:
    """`settings` is the `ingestion` block of PlatformSettings."""
    ext = _ext(file_name)
    res = ValidationResult(ok=False, file_name=file_name, ext=ext,
                           content_hash=sha256(data), size_bytes=len(data))
    parse_ext = set(settings["parse_extensions"])
    text_ext = set(settings["text_extensions"])
    tab_ext = set(settings["tabular_extensions"]) if settings.get("enable_tabular") else set()

    if ext not in parse_ext | text_ext | tab_ext:
        allowed = ", ".join(sorted(parse_ext | text_ext | tab_ext))
        res.reasons.append(f"'.{ext}' files aren't supported. Supported types: {allowed}.")
        return res
    if len(data) == 0:
        res.reasons.append("This file is empty.")
        return res
    if len(data) > settings["max_file_mb"] * 1024 * 1024:
        res.reasons.append(f"This file is larger than {settings['max_file_mb']} MB. Please split it.")

    head = data[:8]
    if ext == "pdf":
        if not head.startswith(PDF_MAGIC):
            res.reasons.append("This file has a .pdf name but isn't a real PDF.")
        else:
            res.page_count, r = _pdf_checks(data)
            res.reasons += r
    elif ext in OOXML_MAIN_PART:
        if head.startswith(OLE_MAGIC):
            res.reasons.append("This file is password protected. Please upload an unprotected copy.")
        elif not head.startswith(ZIP_MAGIC):
            res.reasons.append(f"This file has a .{ext} name but its contents don't match.")
        else:
            res.page_count, r = _ooxml_checks(data, ext)
            res.reasons += r
    elif ext in {"doc", "ppt"}:
        if not head.startswith(OLE_MAGIC):
            res.reasons.append(f"This file has a .{ext} name but its contents don't match.")
    elif ext in IMAGE_MAGIC:
        if not any(head.startswith(m) for m in IMAGE_MAGIC[ext]):
            res.reasons.append(f"This file has a .{ext} name but isn't a real image.")
        res.page_count = 1
    elif ext in text_ext or ext == "csv":
        try:
            data.decode("utf-8")
        except UnicodeDecodeError:
            res.reasons.append("This text file isn't valid UTF-8. Please re-save it as UTF-8.")

    if res.page_count and res.page_count > settings["max_pages"]:
        res.reasons.append(
            f"This document has {res.page_count} pages; the limit is {settings['max_pages']}. "
            "Please split it into smaller files."
        )

    if existing_hashes and res.content_hash in existing_hashes:
        res.duplicate_of = existing_hashes[res.content_hash]
        res.reasons.append(f"This is an exact duplicate of '{res.duplicate_of}', already uploaded.")

    res.parser = ("ai_parse_document" if ext in parse_ext
                  else "text_reader" if ext in text_ext else "tabular")
    res.ok = not res.reasons
    return res
