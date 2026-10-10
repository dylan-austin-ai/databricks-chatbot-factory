"""Extraction QA (QA-1, QA-5, QA-6, QA-7, QA-13, QA-14).

Cheap checks always run and produce a green/yellow/red badge. The visual judge
(Sonnet comparing page images to extracted text) is optional per bot.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field


@dataclass
class QaResult:
    badge: str                                   # green | yellow | red | n/a
    messages: list[str] = field(default_factory=list)  # plain English, per page where possible
    metrics: dict = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps({"badge": self.badge, "messages": self.messages, "metrics": self.metrics})


def classify(metrics: dict, thresholds: dict, expected_pages: int | None = None) -> QaResult:
    """metrics: one row of ingestion.qa_metrics_sql. thresholds: quality.readability."""
    if metrics.get("is_text"):
        # Text files have no parser confidence; show n/a, never "low" (QA-14).
        return QaResult("n/a", ["Text file: read directly, no extraction needed."], metrics)

    msgs: list[str] = []
    badge = "green"

    def worse(b: str) -> None:
        nonlocal badge
        order = {"green": 0, "yellow": 1, "red": 2}
        if order[b] > order[badge]:
            badge = b

    errors = []
    try:
        errors = json.loads(metrics.get("errors_json") or "[]") or []
    except (TypeError, ValueError):
        pass
    for err in errors:
        page = err.get("page_id")
        where = f"page {page + 1}" if isinstance(page, int) else "part of the document"
        msgs.append(f"We couldn't read {where}.")
        worse("red")

    n_pages = int(metrics.get("n_pages") or 0)
    text_pages = set(metrics.get("text_pages") or [])
    if n_pages:
        empty = [i for i in range(n_pages) if i not in text_pages]
        if empty:
            listed = ", ".join(str(i + 1) for i in empty[:10]) + ("…" if len(empty) > 10 else "")
            msgs.append(f"No text was found on page(s) {listed}. They may be scanned images or blank.")
            worse("red" if len(empty) / n_pages > 0.25 else "yellow")
        chars_per_page = (metrics.get("total_chars") or 0) / n_pages
        if chars_per_page < thresholds["min_chars_per_page"]:
            msgs.append("Very little text was extracted for the document's length.")
            worse("yellow")
    if expected_pages and n_pages and n_pages < expected_pages:
        msgs.append(f"The file has {expected_pages} pages but only {n_pages} were processed.")
        worse("red")

    conf = metrics.get("mean_confidence")
    if conf is not None:
        if conf < thresholds["yellow_min_confidence"]:
            msgs.append("The reader was unsure about much of this document.")
            worse("red")
        elif conf < thresholds["green_min_confidence"]:
            msgs.append("Some parts of this document were hard to read.")
            worse("yellow")

    if not msgs:
        msgs.append("Extracted cleanly.")
    return QaResult(badge, msgs, metrics)


def merge_visual(result: QaResult, visual_findings: list[dict]) -> QaResult:
    """Fold visual-judge findings ({page, severity, issue}) into the badge."""
    for f in visual_findings:
        sev = f.get("severity", "minor")
        result.messages.append(f"Page {f.get('page')}: {f.get('issue')}")
        if sev == "major":
            result.badge = "red"
        elif result.badge in ("green", "n/a"):
            result.badge = "yellow"
    return result


# What a reader should make of each quality message, and what to do about it (QA-7). The checks
# differ in how sure they are, so each message gets a kind:
#   problem   the reader reported it; the text is known to be incomplete or blocked
#   check     a measurement suggests trouble; the document may still be fine
#   opinion   an AI reviewer's view of the result; a prompt to look, not a confirmed fault
#   info      nothing wrong with the document
FIX = ("Type the correct text yourself under **The text is wrong? Type it yourself**, below the page. "
       "Or open **Fix a problem** to upload a cleaner copy or re-read it without the bad pages.")
LOOK = "Open **What the chatbot sees** and compare the text with the page."
_GUIDE = [
    (r"We couldn't read ", "problem",
     f"{LOOK} If text is missing, {FIX[0].lower() + FIX[1:]}"),
    (r"The file has \d+ pages but only \d+ were processed", "problem",
     "Some pages were skipped. Check **Fix a problem** for a page range that leaves pages out, then re-read the "
     "document. If the file is very long, split it into smaller files and upload those."),
    (r"The reader was unsure about much of this document", "problem",
     f"The text is probably unreliable, which is common with scans and photos. {LOOK} If it's wrong, upload a copy "
     "exported from the original (Word, PowerPoint) instead of a scan."),
    (r"Blocked: this document appears to contain restricted data", "problem",
     "It won't be used until this is resolved. Remove the restricted data from the source file and upload the "
     "clean copy as a new version. If the match is wrong, ask MLOps."),
    (r"No text was found on page\(s\)", "check",
     "If those pages are blank, covers or pictures, there's nothing to do. If they hold real content, they are "
     f"probably scanned images: {FIX[0].lower() + FIX[1:]}"),
    (r"Very little text was extracted", "check",
     f"Fine for a slide deck or a form; a concern for a text document. {LOOK}"),
    (r"Some parts of this document were hard to read", "check",
     f"{LOOK} Pay attention to tables, small print and footnotes. If it reads correctly, press **Approve**."),
    (r"\d+ section\(s\) contain text that looks like instructions to an AI", "check",
     "Open **Sections**, read the flagged sections, and unflag any that are ordinary content."),
    (r"Contains personal information", "check",
     "Confirm these people's details are meant to be available to everyone who can use this chatbot. If not, "
     "remove them from the source file and upload it again."),
    (r"Page \S+: ", "opinion",
     f"An automatic visual check compared this page with its text and raised this. {LOOK} If the text is right, "
     "ignore it and approve."),
    (r"Reviewer note", "opinion",
     "The automatic reviewer's impression while writing test questions. It is a quality warning, not a confirmed "
     "fault in the file. Skim the document; if the content reads correctly, approve it."),
    (r"Visual check unavailable", "info",
     "That says nothing about the document itself. Review the pages yourself as usual; MLOps can re-run "
     "the check."),
    (r"Extracted cleanly|Text file: read directly", "info", ""),
]


def guidance(message: str) -> dict:
    """{"kind", "text", "next"} for one stored quality message. Works on messages already saved,
    because it reads their wording; an unrecognised message is something to check."""
    import re

    for pattern, kind, next_step in _GUIDE:
        if re.match(pattern, message or ""):
            text = "The automatic visual check couldn't run." if pattern.startswith("Visual check") else message
            return {"kind": kind, "text": text, "next": next_step}
    return {"kind": "check", "text": message, "next": f"{LOOK} If it reads correctly, press **Approve**."}


def reviewer_notes(issues: list) -> list[str]:
    """Stored messages for the automatic reviewer's remarks, each naming its page(s) when the
    reviewer gave them: "Reviewer note (page 2, 3): ...". Accepts the older plain-text form."""
    notes = []
    for item in issues or []:
        if isinstance(item, dict):
            text = str(item.get("issue") or "").strip()
            pages = sorted({int(p) for p in (item.get("pages") or []) if str(p).strip().isdigit() and int(p) > 0})
        else:
            text, pages = str(item).strip(), []
        if text:
            where = f" (page {', '.join(map(str, pages))})" if pages else ""
            notes.append(f"Reviewer note{where}: {text}")
    return notes


def pages_of(message: str) -> set[int]:
    """The 0-based pages a stored quality message is about; empty when it is about the whole
    document or doesn't say."""
    import re

    message = message or ""
    for pattern in (r"We couldn't read page (\d+)", r"^Page (\d+): ",
                    r"No text was found on page\(s\) ([\d, ]+)", r"^Reviewer note \(page ([\d, ]+)\)"):
        found = re.search(pattern, message)
        if found:
            return {int(n) - 1 for n in re.findall(r"\d+", found.group(1)) if int(n) > 0}
    return set()


def page_issues(messages: list[str]) -> tuple[dict[int, list[dict]], list[dict]]:
    """Quality messages sorted for the review screen: {0-based page: [guidance]} for those that
    name pages, and the rest (about the whole document, or with no page given). Messages that
    only say everything is fine are left out."""
    by_page: dict[int, list[dict]] = {}
    general: list[dict] = []
    for message in messages or []:
        note = guidance(message)
        if note["kind"] == "info":
            continue
        pages = pages_of(message)
        for page in sorted(pages):
            by_page.setdefault(page, []).append(note)
        if not pages:
            general.append(note)
    return by_page, general


def next_step(badge: str | None, status: str, messages: list[str]) -> str:
    """One line telling the owner what to do with a document now."""
    kinds = {guidance(m)["kind"] for m in messages}
    if status == "archived":
        return "Archived. Restore it if it should be used again."
    if status == "approved":
        return "Nothing to do. It's approved and in the test version."
    if not badge:
        return "Still being read. Refresh in a few minutes."
    if status == "flagged" or "problem" in kinds:
        return "Fix the problem listed below, or archive the document if it isn't needed."
    if "check" in kinds:
        return "Check the points below. If the text is right, press Approve."
    if "opinion" in kinds:
        return "The automatic reviewer left suggestions. Read them, then press Approve if the document is fine."
    return "Skim it, then press Approve."


VISUAL_JUDGE_PROMPT = """You are checking whether text extraction from a document page is complete and accurate.
You get the page image and the text that was extracted from it.
Report only real problems a reader would care about: missing paragraphs, garbled or
scrambled tables, numbers that differ from the image, headings lost, text from images
or scans that was not captured. Ignore formatting differences and page headers/footers.

Return JSON only: {{"findings": [{{"page": <page number>, "severity": "minor"|"major", "issue": "<one sentence>"}}]}}
Return {{"findings": []}} if the extraction is faithful.

Page number: {page}
Extracted text:
<<<
{text}
>>>"""

GOLDEN_SET_PROMPT = """You are preparing test questions for a company chatbot that answers ONLY from the document below.
The text was extracted automatically and split into sections; [page N] markers show where each section came from.

Chatbot purpose: {purpose}

Tasks:
1. Write {n_easy} realistic, straightforward questions a user might ask that this document answers.
2. Write {n_hard} HARD questions that test whether extraction and chunking worked (EVG-4). Target content
   an automated reader is likely to get wrong: values inside tables, numbers and dates, footnotes,
   figure/chart captions, lists split across pages, exceptions buried in long paragraphs, and facts
   that require combining two sections.
For every question give: the expected answer in 1-3 sentences, an exact supporting quote copied
verbatim from the text (under 40 words), the 1-based page number(s) it comes from, difficulty
("easy" or "hard") and question_type (fact, table, number, footnote, figure, list, multi_section).
3. Separately, note any signs the text was extracted badly: garbled tables, cut-off sentences,
   missing sections, scrambled characters (QA-1). For each one give the 1-based page number(s) where
   it shows, taken from the [page N] markers, so a person can go straight to it. Be specific about
   what is wrong on that page; don't give general impressions of the whole document.

Return JSON only:
{{"questions": [{{"question": "...", "expected_answer": "...", "quote": "...", "expected_pages": [3],
                 "difficulty": "hard", "question_type": "table"}}],
  "extraction_issues": [{{"issue": "...", "pages": [2, 3]}}]}}

Document: {doc_name}
<<<
{text}
>>>"""

OUT_OF_SCOPE_PROMPT = """A company chatbot's purpose is: {purpose}
It must refuse these topics: {refuse}.
Write {n} questions users might plausibly ask that are OUTSIDE its purpose or on refused topics.
Mix near-misses (related but not covered) with clearly off-topic ones.
Return JSON only: {{"questions": ["..."]}}"""
