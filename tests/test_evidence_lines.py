"""Citations name numbered source lines; excerpts are cut from the source and stay verbatim."""
import pathlib

import pytest

from factory import evals
from factory import guardrails as g
from factory.answering import (MAX_CITED_LINES, bounded_excerpt, evidence_units, format_sources, numbered, render,
                               resolve_citations)

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


def test_a_citation_names_lines_and_its_excerpt_is_cut_from_them():
    cited, problems = resolve_citations([{"source": 1, "lines": [2, 3]}, {"source": 2, "lines": ["L4"]}], [PROSE, TABLE])
    assert problems == []
    assert cited[0] == {"source": 1, "lines": [2, 3], "excerpt": "You can use up to 4 hours of confidential, "
                        "action-oriented 1:1 coaching. You can use up to 4 one-hour coaching sessions."}
    assert cited[1]["excerpt"] == "| Coaching | up to 4 hours |"           # one table row, never two merged
    across = resolve_citations([{"source": 1, "lines": [3, 4]}], [PROSE])[0][0]["excerpt"]
    assert across == "You can use up to 4 one-hour coaching sessions.\nThis includes coaching for you and/or your " \
                     "significant other!"                                  # exactly as in the source, newline and all
    assert g.verify_excerpts(cited, [PROSE, TABLE]) == []                  # the word-for-word check still runs


@pytest.mark.parametrize("citation, problem", [
    ({"source": 1, "lines": [9]}, "SOURCE [1] has lines L1 to L6; it has no line 9"),
    ({"source": 1, "lines": [2, 4]}, "must name one line or up to 4 consecutive lines"),
    ({"source": 1, "lines": [1, 2, 3, 4, 5]}, "must name one line or up to 4 consecutive lines"),
    ({"source": 1, "lines": ["two"]}, "must name its lines as numbers"),
    ({"source": 1, "lines": []}, "must name its lines as numbers"),
    ({"source": 3, "lines": [1]}, "citation to source [3] which does not exist"),
    ({"source": True, "lines": [1]}, "which does not exist"),
])
def test_a_citation_that_cannot_be_cut_is_sent_back(citation, problem):
    cited, problems = resolve_citations([citation], [PROSE])
    assert cited == [] and len(problems) == 1 and problem in problems[0]
    assert MAX_CITED_LINES == 4


def test_a_citation_from_an_older_prompt_keeps_its_excerpt_and_is_checked_word_for_word():
    cited, problems = resolve_citations([{"source": 1, "excerpt": "up to 4 two-hour sessions"}], [PROSE])
    assert problems == [] and cited == [{"source": 1, "excerpt": "up to 4 two-hour sessions"}]
    assert g.verify_excerpts(cited, [PROSE]) == ["the excerpt for [1] is not an exact copy of that source"]


def test_a_shortened_excerpt_stays_verbatim_and_the_agent_and_the_quality_check_agree():
    long_line = resolve_citations([{"source": 1, "lines": [2]}], [PROSE])[0][0]["excerpt"]
    excerpt, truncated = bounded_excerpt(long_line, 5)
    assert (excerpt, truncated) == ("You can use up to", True)              # cut at a word, nothing appended
    assert bounded_excerpt(long_line, None) == (long_line, False) and bounded_excerpt(long_line, 50) == (long_line, False)
    # What the agent stores for that citation, checked by the quality check's own validator:
    custom = {"retrieved_chunk_ids": ["c1"], "citations": [
        {"n": 1, "chunk_id": "c1", "excerpt": excerpt, "truncated": truncated, "doc_name": "Benefits Guide",
         "doc_version": 3, "pages": "4", "section": None}]}
    answer = 'You can use "up to 4 one-hour coaching sessions" [1].'
    assert evals.citation_check(answer, custom, {"c1": PROSE}) == (True, [])
    assert g.verify_excerpts([{"source": 1, "excerpt": excerpt}], [PROSE]) == []
    # The ellipsis is shown, not stored. Stored, it was what failed every one of those citations:
    assert render(answer, custom["citations"], []).endswith("“You can use up to …”")
    stored_with_it = {**custom, "citations": [{**custom["citations"][0], "excerpt": excerpt + " …"}]}
    assert evals.citation_check(answer, stored_with_it, {"c1": PROSE})[0] is False


def test_the_agent_uses_the_lines_keeps_excerpts_verbatim_and_repairs_through_the_system_message():
    agent = (ROOT / "agent" / "agent.py").read_text()
    assert "cited, line_problems = resolve_citations(parsed.get(\"citations\") or [], sources_text)" in agent
    assert "citations=cited)" in agent and "for c in cited:" in agent
    assert "excerpt, truncated = bounded_excerpt(c[\"excerpt\"]," in agent and '"truncated": truncated,' in agent
    assert "cap_quotes(f'\"{excerpt}\"'" not in agent                      # the ellipsis that was stored
    assert '{"role": "user", "content": "Revise your reply. "' not in agent  # repair never posed as the user
    assert 'messages = [{"role": "system", "content": first[0]["content"] + "\\n\\nYOUR PREVIOUS REPLY FAILED THE "' in agent
    assert "+ first[1:]" in agent and '{"role": "assistant", "content": raw}' not in agent
    chat = (ROOT / "app" / "pages" / "chat.py").read_text()
    assert 'more = " …" if c.get("truncated") else ""' in chat


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
