"""What the search index matches on, kept apart from what the chatbot quotes (RET-1, ING-9).

Two problems from the QA audit. A section that continues a table had lost the table's header row,
so amounts survived but the coverage levels they belong to did not, and the model rightly
declined to pair them up. And the searchable text of some screenshots was mostly the same icon
descriptions repeated, with no hint of which system or document it came from.

Both are fixed when a document is chunked, so they reach the shared index on its next sync:

* A section that starts in the middle of a table gets the table's header lines from the section
  before it: the document's own words, so quotes stay verbatim, with that section's pages added
  to its pages. (The evidence text, chunk_to_retrieve, changes only by those header lines.)
* The searchable text (chunk_to_embed) starts with the document's title and keeps each repeated
  line once. The evidence text is not touched by this.
"""
from __future__ import annotations

import json
import re

MAX_HEADER_LINES = 3
_MD_SEPARATOR = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$")


def _lines(text: str) -> list[str]:
    return [line for line in (text or "").splitlines() if line.strip()]


def table_header(text: str) -> str:
    """The header of the last table in `text` (a markdown header row and its separator, or an
    HTML row of <th> cells), or "" if it has none."""
    lines = (text or "").splitlines()
    for i in range(len(lines) - 1, 0, -1):
        if _MD_SEPARATOR.match(lines[i]) and lines[i - 1].lstrip().startswith("|"):
            return lines[i - 1] + "\n" + lines[i]
    heads = re.findall(r"<tr>(?:(?!</tr>).)*?<th[\s>](?:(?!</tr>).)*</tr>", text or "", re.I | re.S)
    return heads[-1] if heads else ""


def continues_a_table(text: str) -> bool:
    """Whether a section starts with table rows and has no header of its own."""
    lines = _lines(text)
    if not lines:
        return False
    first = lines[0].lstrip()
    if first.startswith("|"):
        return not any(_MD_SEPARATOR.match(line) for line in lines[:MAX_HEADER_LINES + 1])
    return first.lower().startswith("<tr") and "<th" not in "".join(lines[:2]).lower()


def title(doc_name: str) -> str:
    """A document's name as a title: no extension, underscores and dashes as spaces."""
    return re.sub(r"\s+", " ", re.sub(r"[_\-]+", " ", re.sub(r"\.[A-Za-z0-9]{1,5}$", "", doc_name or ""))).strip()


def once_each(text: str) -> str:
    """The text with each repeated line kept once (compared ignoring case and spacing)."""
    seen, out = set(), []
    for line in (text or "").splitlines():
        key = re.sub(r"\s+", " ", line).strip().lower()
        if key and key in seen:
            continue
        seen.add(key)
        out.append(line)
    return "\n".join(out)


def searchable(doc_name: str, embed_text: str) -> str:
    """What the index matches on: the document's title first, then the text with repeats removed."""
    name = title(doc_name)
    body = once_each(embed_text)
    return f"Document: {name}\n{body}" if name and not body.startswith(f"Document: {name}\n") else body


def _pages(value) -> list[int]:
    if isinstance(value, str):   # the warehouse returns arrays as JSON text
        value = json.loads(value or "[]")
    return [int(x) for x in (value or [])]


def refine(chunks: list[dict]) -> list[dict]:
    """The changes for one document version's chunks (each with chunk_id, chunk_position,
    doc_name, page_ids, chunk_to_retrieve, chunk_to_embed): {chunk_id, chunk_to_retrieve,
    chunk_to_embed, page_ids} for every chunk whose text should change."""
    out, header, header_pages = [], "", []
    for c in sorted(chunks, key=lambda c: int(c.get("chunk_position") or 0)):
        text, pages = c.get("chunk_to_retrieve") or "", _pages(c.get("page_ids"))
        embed = c.get("chunk_to_embed") or text
        if header and continues_a_table(text) and header not in text:
            text = header + "\n" + text
            embed = header + "\n" + embed
            pages = sorted(set(pages) | set(header_pages))
        new_embed = searchable(c.get("doc_name") or "", embed)
        if text != c.get("chunk_to_retrieve") or new_embed != c.get("chunk_to_embed") or pages != _pages(c.get("page_ids")):
            out.append({"chunk_id": c["chunk_id"], "chunk_to_retrieve": text, "chunk_to_embed": new_embed,
                        "page_ids": pages})
        found = table_header(c.get("chunk_to_retrieve") or "")
        if found:
            header, header_pages = found, _pages(c.get("page_ids"))
        elif not continues_a_table(c.get("chunk_to_retrieve") or ""):
            header, header_pages = "", []   # the table ended; don't carry its header further
    return out
