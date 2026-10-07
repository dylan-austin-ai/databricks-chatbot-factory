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
   missing sections, scrambled characters (QA-1).

Return JSON only:
{{"questions": [{{"question": "...", "expected_answer": "...", "quote": "...", "expected_pages": [3],
                 "difficulty": "hard", "question_type": "table"}}],
  "extraction_issues": ["..."]}}

Document: {doc_name}
<<<
{text}
>>>"""

OUT_OF_SCOPE_PROMPT = """A company chatbot's purpose is: {purpose}
It must refuse these topics: {refuse}.
Write {n} questions users might plausibly ask that are OUTSIDE its purpose or on refused topics.
Mix near-misses (related but not covered) with clearly off-topic ones.
Return JSON only: {{"questions": ["..."]}}"""
