"""Citations name numbered source lines; excerpts are cut from the source and stay verbatim."""
import pathlib

import pytest

from factory import evals
from factory import guardrails as g
from factory.answering import (MAX_CITED_LINES, MAX_SPANS, bounded_excerpt, evidence_units, format_sources, numbered,
                               quote_hints, render, resolve_citations)

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
    ({"source": 1, "lines": [1, 2, 3, 4, 5]}, "names too much: at most 4 places in the source, each at most 4"),
    ({"source": 1, "lines": ["two"]}, "must name its lines as numbers"),
    ({"source": 1, "lines": []}, "must name its lines as numbers"),
    ({"source": 3, "lines": [1]}, "citation to source [3] which does not exist"),
    ({"source": True, "lines": [1]}, "which does not exist"),
])
def test_a_citation_that_cannot_be_cut_is_sent_back(citation, problem):
    cited, problems = resolve_citations([citation], [PROSE])
    assert cited == [] and len(problems) == 1 and problem in problems[0]
    assert MAX_CITED_LINES == 4 and MAX_SPANS == 4


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


# The QA coaching answer: it quoted "up to 4 hours" and cited lines 16, 29 and 30. Consecutive lines
# only, it was withheld, and the second try named the same lines. (A 30-line source shaped like the
# one in that trace; the policy text itself isn't in the repo.)
COACHING = "\n".join(
    [f"Line {k} of the benefits guide." for k in range(1, 16)]
    + ["Working Parent Career Coaching: you can use up to 4 hours of confidential, action-oriented 1:1 coaching."]
    + [f"Line {k} of the benefits guide." for k in range(17, 29)]
    + ["You can use up to 4 one-hour coaching sessions.",
       "This includes coaching for you and/or your significant other."])


def test_the_coaching_answer_cites_lines_that_are_apart_and_each_place_is_cut_exactly():
    assert len(evidence_units(COACHING)) == 30
    cited, problems = resolve_citations([{"source": 1, "lines": [16, 29, 30]}], [COACHING])
    assert problems == []
    assert [(c["lines"], c["excerpt"]) for c in cited] == [
        ([16], "Working Parent Career Coaching: you can use up to 4 hours of confidential, action-oriented 1:1 coaching."),
        ([29, 30], "You can use up to 4 one-hour coaching sessions.\nThis includes coaching for you and/or your "
                   "significant other.")]
    answer = ("You can use “up to 4 hours of confidential, action-oriented 1:1 coaching”, that is “up to 4 one-hour "
              "coaching sessions” [1]. “This includes coaching for you and/or your significant other” [1].")
    check = g.check_answer(answer, [COACHING], "system prompt", False, 60, citations=cited)
    assert check.ok and check.problems == []
    stored = {"retrieved_chunk_ids": ["c1"], "citations": [{"n": 1, "chunk_id": "c1", **c} for c in cited]}
    assert evals.citation_check(answer, stored, {"c1": COACHING}) == (True, [])   # and the quality check agrees


def test_a_citation_may_not_ask_for_more_than_four_places():
    many = resolve_citations([{"source": 1, "lines": [1, 3, 5, 7, 9]}], [COACHING])
    assert many[0] == [] and "at most 4 places" in many[1][0]
    four = resolve_citations([{"source": 1, "lines": [1, 3, 5, 7]}], [COACHING])
    assert four[1] == [] and len(four[0]) == 4


@pytest.mark.parametrize("reply, says", [
    ("Here is my answer: the coaching is 4 hours.", "no JSON object"),
    ('{"status": "answered", "answer": "You can use "up to 4 hours" [1].", "citations": []}', "not valid JSON"),
    ('{"status": "answered", "answer": "cut off', "not valid JSON"),
    ('{"answer": "x"}', 'no "status"'),
])
def test_a_reply_that_is_not_one_of_the_json_forms_gets_a_specific_reason(reply, says):
    assert g.parse_structured(reply) is None
    assert says in g.json_problem(reply)


def test_curly_quotes_keep_the_json_valid_and_are_still_checked_word_for_word():
    reply = '{"status": "answered", "answer": "You can use “up to 4 one-hour coaching sessions” [1].", "citations": []}'
    assert g.parse_structured(reply)["answer"].startswith("You can use “up to")
    assert g.json_problem(reply) == ""
    assert g.verify_quotes("You can use “up to 4 two-hour coaching sessions” [1].", [COACHING]) == [
        "up to 4 two-hour coaching sessions"]


def test_the_second_try_is_told_which_line_a_quote_was_probably_from():
    hints = quote_hints(["you can use up to 4 one hour coaching session"], [PROSE, COACHING])
    assert len(hints) == 1 and "the closest line is SOURCE [" in hints[0]
    assert "You can use up to 4 one-hour coaching sessions." in hints[0]
    assert quote_hints(["nothing at all like any line here, zebra quantum"], [PROSE]) == []   # no guess when none is close


def test_a_gateway_placeholder_in_a_quote_is_named_as_the_gateways_doing():
    source = "Call the benefits line on 943 476 5919 and quote your plan number."
    assert g.gateway_placeholders("on [UK_NHS] and quote", [source]) == ["[UK_NHS]"]
    assert g.gateway_placeholders("see [NOTE]", ["[NOTE] is in the source"]) == []          # a source's own label
    hints = quote_hints(["Call the benefits line on [UK_NHS]"], [source])
    assert hints == ["“Call the benefits line on [UK_NHS]” contains [UK_NHS]: text the gateway's sensitive-data policy "
                     "put in place of something in the source. Leave that part out of the quote."]


def test_the_agent_repairs_with_the_previous_reply_and_reports_why_an_answer_was_withheld():
    agent = (ROOT / "agent" / "agent.py").read_text()
    assert '+ " ".join(problems) + "\\n\\nYour previous reply was:\\n" + raw[:4000]}] + first[1:]' in agent
    assert "g.json_problem(raw)" in agent and "quote_hints(g.verify_quotes(" in agent
    assert 'log["debug"]["unverified"] = {"kinds": sorted(set(kinds)),' in agent
    assert ('"unverified": (log["debug"].get("unverified") if log["outcome"] == "unverified"\n'
            '                               and log.get("synthetic") else None),') in agent   # platform callers only
    assert '_event(log, "gateway_redaction", "flag",' in agent
