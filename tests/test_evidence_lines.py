"""Citations name numbered source lines; excerpts are cut from the source and stay verbatim."""
import pathlib

import pytest

from factory import answer_contract as contract
from factory import evals
from factory.answering import MAX_CITED_LINES, bounded_excerpt, evidence_units, format_sources, numbered, render

ROOT = pathlib.Path(__file__).resolve().parents[1]
PROSE = ("Working Parent Career Coaching. You can use up to 4 hours of confidential, action-oriented 1:1 coaching. "
         "You can use up to 4 one-hour coaching sessions.\nThis includes coaching for you and/or your significant "
         "other! Book through the portal?  Yes.")
TABLE = "Plan limits:\n| Benefit | Limit |\n|---|---|\n| Coaching | up to 4 hours |\n| Milk Stork | $1,000 per trip |"
HTML = "<table><tr><td>Coaching</td><td>4 hours</td></tr><tr><td>Milk Stork</td><td>$1,000</td></tr></table>"


def units(text):
    return [text[a:b] for a, b in evidence_units(text)]


def test_prose_is_numbered_by_sentence_and_every_line_is_an_exact_piece_of_the_source():
    assert units(PROSE) == [
        "Working Parent Career Coaching.", "You can use up to 4 hours of confidential, action-oriented 1:1 coaching.",
        "You can use up to 4 one-hour coaching sessions.",
        "This includes coaching for you and/or your significant other!", "Book through the portal?", "Yes."]
    for text in (PROSE, TABLE, HTML, "", "   \n\n"):
        for a, b in evidence_units(text):
            assert text[a:b] and text[a:b] == text[a:b].strip()
    assert numbered(PROSE).splitlines()[2] == "L3: You can use up to 4 one-hour coaching sessions."


def test_a_table_row_is_one_line_whether_markdown_or_html():
    assert units(TABLE) == ["Plan limits:", "| Benefit | Limit |", "|---|---|", "| Coaching | up to 4 hours |",
                            "| Milk Stork | $1,000 per trip |"]
    assert units(HTML) == ["<table><tr><td>Coaching</td><td>4 hours</td></tr>",
                           "<tr><td>Milk Stork</td><td>$1,000</td></tr>", "</table>"]


def test_the_model_sees_numbered_lines():
    shown = format_sources([{"doc_name": "Benefits Guide", "doc_version": 3, "section": "Coaching"}], [PROSE],
                           lambda h: "4")
    assert shown.startswith("<<<SOURCE [1] document: Benefits Guide (version 3) | page(s): 4 | section: Coaching\nL1: ")
    assert "\nL6: Yes.\n>>>" in shown


def test_a_shortened_excerpt_stays_verbatim_and_the_agent_and_the_quality_check_agree():
    spans, _ = contract.resolve_evidence([{"source": 1, "lines": [2]}], [PROSE])
    long_line = spans[0]["excerpt"]
    excerpt, truncated = bounded_excerpt(long_line, 5)
    assert (excerpt, truncated) == ("You can use up to", True)              # cut at a word, nothing appended
    assert bounded_excerpt(long_line, None) == (long_line, False) and bounded_excerpt(long_line, 50) == (long_line, False)
    # What the agent stores for that citation, checked by the quality check's own validator:
    custom = {"retrieved_chunk_ids": ["c1"], "citations": [
        {"n": 1, "chunk_id": "c1", "excerpt": excerpt, "truncated": truncated, "doc_name": "Benefits Guide",
         "doc_version": 3, "pages": "4", "section": None}],
        "blocks": [{"text": 'You can use "up to 4 hours of confidential, action-oriented 1:1 coaching".', "cites": [0]}]}
    answer = contract.render_text(custom["blocks"], [{"source": 1}])
    assert answer.endswith(" [1]") and evals.citation_check(answer, custom, {"c1": PROSE}) == (True, [])
    # The ellipsis is shown, not stored. Stored, it was what failed every one of those citations:
    assert render(answer, custom["citations"], []).endswith("“You can use up to …”")
    stored_with_it = {**custom, "citations": [{**custom["citations"][0], "excerpt": excerpt + " …"}]}
    assert evals.citation_check(answer, stored_with_it, {"c1": PROSE})[0] is False
    assert MAX_CITED_LINES == 4


def test_correctness_is_skipped_without_a_ground_truth_and_duplicate_questions_stay_apart():
    pytest.importorskip("mlflow.genai.scorers")
    from factory.tracing import assessment_value

    results, status, traces = {}, {}, {}
    calls = []
    judge = evals.recording_scorer("correctness", lambda **kw: calls.append(kw["expectations"]) or "yes", results,
                                   status, assessment_value, ("expected_response", "expected_facts"), str, traces)
    refuse = evals.dataset_record({"kind": "out_of_scope", "eval_id": "e9", "question": "Is my claim covered?"})
    assert judge.run(inputs=refuse["inputs"], outputs="no", expectations=refuse["expectations"], trace=None) is None
    assert status["e9"] == {"correctness": "skipped"} and calls == []       # no error from a missing ground truth
    facts = {"kind": "in_scope", "eval_id": "e10", "expected_facts": ["4 one-hour sessions"]}
    assert judge.run(inputs={"question": "q"}, outputs="a", expectations=facts, trace=None) == "yes"
    # the same question twice, as two test questions: two records, not one overwritten
    for eid in ("e1", "e2"):
        exp = {"kind": "in_scope", "eval_id": eid, "expected_response": "4 hours"}
        judge.run(inputs={"question": "How many hours?"}, outputs="4 hours", expectations=exp,
                  trace=type("T", (), {"info": type("I", (), {"trace_id": f"tr-{eid}"})()})())
    assert {"e1", "e2", "e10"} <= set(results) and traces == {"e1": "tr-e1", "e2": "tr-e2"}
    job = (ROOT / "jobs" / "run_evals.py").read_text()
    assert "OUTCOMES[mine], AGENT_TRACES[mine] = " in job and "OUTCOMES[question]" not in job
    assert "return OUTCOMES.get(TRACE_OF.get(case[\"eval_id\"], \"\"), \"\")" in job


