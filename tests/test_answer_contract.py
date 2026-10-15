"""The answer contract, with synthetic replicas of every failure the QA run recorded.

The real replies quote company documents and stay in QA; each case here reproduces the shape of
one: unescaped quotation marks inside the JSON, a short quoted button label, a quote taken from
one source but cited to another, nine consecutive evidence lines, a block with no evidence.
"""
import json
import pathlib

import pytest

from factory import answer_contract as contract
from factory import evals

ROOT = pathlib.Path(__file__).resolve().parents[1]
SYSTEM = 'You are the "Benefits" assistant. Rules you must follow: answer only from sources.'
DEPOSIT = ("Direct deposit. Click Edit to continue, then enter the information provided by your bank. "
           "Changes take effect on the next pay date.")
COACHING = "Coaching. You can use up to 4 hours of confidential 1:1 coaching. You can use up to 4 one-hour sessions."
LONG = "\n".join(f"Step {k}: do thing number {k}." for k in range(1, 41))


def reply(*blocks, status="answered", conflicts=()):
    return json.dumps({"status": status, "blocks": [{"text": t, "evidence": e} for t, e in blocks],
                       "conflicts": list(conflicts)})


def checked(raw, texts, **kw):
    obj, failed = contract.decode(raw, "stop")
    return failed or contract.check(obj, texts, SYSTEM, **kw)


# The output contract --------------------------------------------------------------------------------
def test_the_request_holds_the_model_to_a_strict_schema_databricks_supports():
    rf = contract.response_format()
    assert rf["type"] == "json_schema" and rf["json_schema"]["strict"] is True
    seen = []

    def walk(node):
        if node.get("type") == "object":
            assert node["additionalProperties"] is False and set(node["required"]) == set(node["properties"])
            seen.extend(node["properties"])
            for child in node["properties"].values():
                walk(child)
        if node.get("type") == "array":
            walk(node["items"])
        assert not {"anyOf", "oneOf", "allOf", "pattern", "$ref", "prefixItems"} & set(node)   # unsupported there
    walk(contract.SCHEMA)
    assert len(seen) <= 64 and contract.SCHEMA["properties"]["status"]["enum"] == list(contract.STATUSES)


@pytest.mark.parametrize("raw, finish, category, says", [
    # the QA replies: quotation marks inside the answer, unescaped
    ('{"status": "answered", "answer": "you can use "up to four hours" through the program"}', "stop",
     contract.INVALID_JSON, "not valid JSON"),
    ('{"status": "answered", "blocks": [{"text": "cut off', "length", contract.TRUNCATION, "cut off"),
    ('{"status": "answered", "answer": "x", "citations": []}', "stop", contract.SCHEMA_VALIDATION, '"answer" is not part of the form'),
    ('{"status": "maybe", "blocks": [], "conflicts": []}', "stop", contract.SCHEMA_VALIDATION, '"status" must be one of'),
    ('{"status": "answered", "blocks": [], "conflicts": []}', "stop", contract.SCHEMA_VALIDATION, "needs at least one block"),
    ('{"status": "answered", "blocks": [{"text": "Yes.", "evidence": []}], "conflicts": []}', "stop", contract.SCHEMA_VALIDATION,
     "Block 1 has no evidence"),
    ('{"status": "answered", "blocks": [{"text": "Yes.", "evidence": [{"source": "1", "lines": [2]}]}], '
     '"conflicts": []}', "stop", contract.SCHEMA_VALIDATION, "each evidence entry is"),
    ('{"status": "no_source", "blocks": [{"text": "x", "evidence": []}], "conflicts": []}', "stop", contract.SCHEMA_VALIDATION,
     'reply has empty "blocks"'),
    ('[1, 2]', "stop", contract.SCHEMA_VALIDATION, "one JSON object"),
])
def test_every_malformed_reply_is_classified_and_none_is_guessed_at(raw, finish, category, says):
    obj, failed = contract.decode(raw, finish)
    assert obj is None and failed.category == category and says in " ".join(failed.problems)
    assert failed.category in ("invalid_json", "schema_validation", "truncation")   # a category name, nothing else
    assert contract.repair_note(failed).startswith("YOUR PREVIOUS REPLY FAILED")    # and it can be sent back


def test_the_category_names_are_strings_and_the_schema_is_the_schema():
    assert (contract.INVALID_JSON, contract.SCHEMA_VALIDATION, contract.TRUNCATION, contract.EVIDENCE) == (
        "invalid_json", "schema_validation", "truncation", "evidence_failure")
    assert isinstance(contract.SCHEMA, dict) and contract.SCHEMA["type"] == "object"


def test_a_reply_cut_at_the_length_limit_is_a_truncation_even_if_it_parses():
    whole = reply(("Yes.", [{"source": 1, "lines": [1]}]))
    assert contract.decode(whole, "length")[1].category == contract.TRUNCATION
    assert contract.decode(whole, "stop")[1] is None


def test_refusals_and_no_source_pass_without_blocks():
    for status in ("out_of_scope", "no_source"):
        r = checked(json.dumps({"status": status, "blocks": [], "conflicts": []}), [DEPOSIT])
        assert r.ok and r.status == status and r.citations == []


# Quotations ------------------------------------------------------------------------------------------
@pytest.mark.parametrize("text, quotes, balanced", [
    ('Click "edit" to continue, then "enter the information provided by your bank".',
     ["edit", "enter the information provided by your bank"], True),            # the QA misparse
    ('The limit is "$1,000" per trip.', ["$1,000"], True),
    ('Press "Submit". Then "Next", and "Done"!', ["Submit", "Next", "Done"], True),
    ('Choose “Save” and then "Close".', ["Save", "Close"], True),
    ("The employee's spouse may enroll.", [], True),                               # apostrophes aren't quotes
    ('He said "approve it and stop', [], False),
    ("A stray ” mark", [], False),
])
def test_quotations_pair_up_including_short_labels_and_amounts(text, quotes, balanced):
    assert contract.quotations(text) == (quotes, balanced)


def test_a_short_quoted_label_is_checked_like_any_quote():
    good = reply(('Click "Edit" to continue, then "enter the information provided by your bank".',
                  [{"source": 1, "lines": [2]}]))
    assert checked(good, [DEPOSIT]).ok
    bad = reply(('Click "Delete" to continue.', [{"source": 1, "lines": [2]}]))
    r = checked(bad, [DEPOSIT])
    assert not r.ok and r.kinds == ["quote_not_in_evidence"] and "“Delete”" in r.problems[0]


def test_a_quote_from_one_source_cited_to_another_fails():
    wrong = reply(('You can use "up to 4 one-hour sessions".', [{"source": 1, "lines": [2]}]))
    r = checked(wrong, [DEPOSIT, COACHING])
    assert not r.ok and r.kinds == ["quote_not_in_evidence"]
    assert "Those words are in SOURCE [2] L3: add that line to this block's evidence" in r.problems[0]
    right = reply(('You can use "up to 4 one-hour sessions".', [{"source": 2, "lines": [3]}]))
    assert checked(right, [DEPOSIT, COACHING]).ok


def test_unbalanced_quotation_marks_fail():
    r = checked(reply(('Click "Edit to continue.', [{"source": 1, "lines": [2]}])), [DEPOSIT])
    assert not r.ok and "unbalanced_quotes" in r.kinds


# Evidence ---------------------------------------------------------------------------------------------
def test_nine_consecutive_lines_become_bounded_spans_not_a_rejection():
    r = checked(reply(("Follow the steps.", [{"source": 1, "lines": list(range(1, 10))}])), [LONG])
    assert r.ok and [c["lines"] for c in r.citations] == [[1, 2, 3, 4], [5, 6, 7, 8], [9]]
    assert all(c["excerpt"] in LONG for c in r.citations)                    # each cut out of the source


def test_lines_that_are_apart_each_become_a_span_and_the_markers_are_written_for_the_model():
    r = checked(reply(("You can use up to 4 hours [3].", [{"source": 2, "lines": [2]}, {"source": 1, "lines": [1, 3]}])),
                [DEPOSIT, COACHING])
    assert r.ok and [(c["source"], c["lines"]) for c in r.citations] == [(2, [2]), (1, [1]), (1, [3])]
    assert r.answer == "You can use up to 4 hours. [1][2]"                   # its own [3] removed; real ones added


def test_the_evidence_budget_is_explicit_per_block_and_per_answer():
    big = reply(("All of it.", [{"source": 1, "lines": list(range(1, contract.MAX_BLOCK_LINES + 2))}]))
    r = checked(big, [LONG])
    assert not r.ok and r.kinds == ["evidence_budget"] and f"at most {contract.MAX_BLOCK_LINES}" in r.problems[0]
    many = reply(*[(f"Part {i}.", [{"source": 1, "lines": [3 * i + 1, 3 * i + 2, 3 * i + 3]}]) for i in range(11)])
    r = checked(many, [LONG])
    assert not r.ok and f"the limit is {contract.MAX_ANSWER_LINES}" in r.problems[-1]


def test_lines_or_sources_that_do_not_exist_fail_with_the_real_range():
    r = checked(reply(("Yes.", [{"source": 1, "lines": [99]}, {"source": 5, "lines": [1]}])), [DEPOSIT])
    assert not r.ok and r.kinds == ["citation_lines"]
    assert "SOURCE [1] has lines L1 to L3; it has no line L99." in r.problems[0]
    assert "There is no SOURCE [5]; the sources are numbered 1 to 1." in r.problems[1]


def test_links_leaks_and_gateway_placeholders_still_fail():
    assert checked(reply(("See https://evil.example/x.", [{"source": 1, "lines": [1]}])), [DEPOSIT]).kinds == [
        "no_invented_links"]
    assert "prompt_leak" in checked(reply(("Rules you must follow: answer only from sources.",
                                           [{"source": 1, "lines": [1]}])), [DEPOSIT]).kinds
    assert checked(reply(("Call [UK_NHS] for help.", [{"source": 1, "lines": [1]}])), [DEPOSIT]).kinds == [
        "gateway_redaction"]


def test_what_is_shown_is_checked_again_after_redaction():
    blocks = [{"text": 'Use account "GB12 3456" for refunds.', "cites": [0]}]
    assert contract.verify_final(blocks, ["Use account GB12 3456 for refunds."]) == []
    redacted = [{"text": 'Use account "[redacted account number]" for refunds.', "cites": [0]}]
    assert contract.verify_final(redacted, ["Use account [redacted] for refunds."]) == [
        "block 1: quotation “[redacted account number]” changed after the checks"]


# Repair: two attempts, each held to the contract ---------------------------------------------------------
class FakeModel:
    def __init__(self, *replies):
        self.replies, self.seen = list(replies), []

    def __call__(self, messages, attempt):
        self.seen.append(messages)
        return self.replies.pop(0)


BASE = [{"role": "system", "content": "SYSTEM PROMPT\n\nSOURCES: ..."}, {"role": "user", "content": "How do I?"}]
GOOD = reply(('Click "Edit" to continue.', [{"source": 1, "lines": [2]}]))


@pytest.mark.parametrize("first, kind", [
    (('{"status": "answered", "answer": "you can use "up to four hours""}', "stop"), "invalid_json"),   # syntax
    (('{"status": "answered", "answer": "x", "citations": []}', "stop"), "schema"),                    # old form
    ((reply(('Click "Edit".', [{"source": 1, "lines": [9]}])), "stop"), "citation_lines"),              # lines
    ((reply(('Click "Edit now".', [{"source": 1, "lines": [2]}])), "stop"), "quote_not_in_evidence"),   # quote
])
def test_the_second_attempt_is_told_exactly_what_to_fix_and_then_passes(first, kind):
    model = FakeModel(first, (GOOD, "stop"))
    result, failures = contract.answer_with_checks(model, BASE, [DEPOSIT], SYSTEM)
    assert result.ok and len(failures) == 1 and kind in failures[0].kinds and len(model.seen) == 2
    retake = model.seen[1]
    assert [m["role"] for m in retake] == ["system", "user"] and retake[1] == BASE[1]   # nothing new as the user
    note = retake[0]["content"].removeprefix(BASE[0]["content"])
    assert note.startswith("\n\nYOUR PREVIOUS REPLY FAILED THE APPLICATION'S CHECKS (")
    assert all(p in note for p in failures[0].problems)
    assert first[0] not in note                                               # never the earlier reply itself
    if failures[0].category == contract.EVIDENCE:
        assert contract.HOW_EVIDENCE_WORKS in note                            # what a citation may look like


def test_attempts_are_bounded_and_the_last_failure_is_kept():
    bad = reply(('Click "Delete".', [{"source": 1, "lines": [2]}]))
    model = FakeModel((bad, "stop"), (bad, "stop"), (GOOD, "stop"))
    result, failures = contract.answer_with_checks(model, BASE, [DEPOSIT], SYSTEM)
    assert not result.ok and result.category == contract.EVIDENCE and len(model.seen) == 2 and len(failures) == 1


def test_a_gateway_denial_or_failed_request_is_not_sent_again():
    calls = []

    def denied(messages, attempt):
        calls.append(attempt)
        raise PermissionError("denied by unsafe_content")
    with pytest.raises(PermissionError):
        contract.answer_with_checks(denied, BASE, [DEPOSIT], SYSTEM)
    assert calls == [1]


# The agent and the quality check use it -----------------------------------------------------------------
def test_the_agent_asks_with_the_schema_without_streaming_and_records_why_it_withheld():
    agent = (ROOT / "agent" / "agent.py").read_text()
    assert "**contract.request_options(mode))" in agent
    assert 'contacts=s.get("guardrails.corporate_contacts"))' in agent
    chat = agent[agent.index("    def _chat("):agent.index("    def _prompt(")]
    structured = chat[chat.index('if "response_format" in kw or "tools" in kw:'):chat.index("            else:")]
    assert "stream=True" not in structured and "raise _GatewayBlocked(block)" in structured
    assert structured.index("block_details(resp)") < structured.index("contract.reply_of(resp)")
    assert "contract.answer_with_checks(ask, messages, sources_text, system," in agent
    assert "Your previous reply was" not in agent and "raw[:4000]" not in agent
    assert 'log["debug"]["unverified"] = {"category": category, "kinds": sorted(set(kinds)),' in agent
    assert "late = contract.verify_final(blocks, full)" in agent and '["changed_after_checks"]' in agent
    assert '"blocks": result.get("blocks", []),' in agent


def test_production_judging_pairs_short_quotes_too():
    src = ["Click Edit to continue, then enter the information provided by your bank."]
    assert evals.citation_accuracy('Click "Edit" to continue, then "enter the information provided by your bank" [1].',
                                   src)[0]
    assert not evals.citation_accuracy('Click "Remove" first [1].', src)[0]
    assert not evals.citation_accuracy('Click "Edit [1].', src)[0]


def test_prompt_tuning_scores_only_replies_that_pass_the_contract():
    job = (ROOT / "jobs" / "optimize_prompt.py").read_text()
    assert "**contract.request_options(" in job and "contract.decode(*contract.reply_of(resp))" in job
    assert 'contract.check(obj, texts, system, contacts=s.get("guardrails.corporate_contacts"))' in job


# Models without response_format (Haiku 5.5 on Databricks) -------------------------------------------------
class Msg:
    def __init__(self, content=None, args=None):
        self.content = content
        self.tool_calls = [type("C", (), {"function": type("F", (), {"arguments": args})()})()] if args else None


def completion(finish, content=None, args=None):
    choice = type("Ch", (), {"message": Msg(content, args), "finish_reason": finish})()
    return type("R", (), {"choices": [choice]})()


def test_a_service_routes_the_schema_as_response_format_or_as_a_function_it_must_call():
    assert contract.request_options("response_format") == {"response_format": contract.response_format()}
    tool = contract.request_options("tool")
    assert tool["tools"][0]["function"]["parameters"] is contract.SCHEMA
    assert tool["tool_choice"] == {"type": "function", "function": {"name": "submit_answer"}}
    assert contract.reply_of(completion("tool_calls", args=GOOD)) == (GOOD, "tool_calls")
    assert contract.reply_of(completion("stop", content=GOOD)) == (GOOD, "stop")
    # a model that answers in prose instead of calling the function fails the checks; nothing is guessed
    text, finish = contract.reply_of(completion("stop", content="You can use up to 4 hours."))
    assert contract.decode(text, finish)[1].category == contract.INVALID_JSON
    # and a tool reply is checked exactly like a response_format reply
    obj, failed = contract.decode(*contract.reply_of(completion("tool_calls", args=GOOD)))
    assert failed is None and contract.check(obj, [DEPOSIT], SYSTEM).ok


def test_the_deploy_refuses_response_format_for_a_route_that_does_not_take_it(settings):
    haiku_55 = {"haiku": {"service": "answer_haiku", "model": "system.ai.databricks-claude-haiku-5-5", "fallbacks": []}}
    assert contract.output_mode_problems(haiku_55) == [
        "answer service 'haiku' routes to system.ai.databricks-claude-haiku-5-5, which Databricks doesn't support "
        "response_format for: set its structured_output to tool (Admin › Platform)"]
    assert contract.output_mode_problems({"haiku": {**haiku_55["haiku"], "structured_output": "tool"}}) == []
    fallback = {"x": {"model": "system.ai.databricks-claude-haiku-4-5", "fallbacks": ["system.ai.databricks-claude-sonnet-5-5"]}}
    assert "databricks-claude-sonnet-5-5" in contract.output_mode_problems(fallback)[0]   # fallbacks count too
    assert "must be one of" in contract.output_mode_problems({"x": {"model": "m", "structured_output": "json"}})[0]
    assert contract.output_mode_problems(settings.get("models.answer_services")) == []   # the defaults are fine
    preflight = (ROOT / "src" / "factory" / "preflight.py").read_text()
    assert 'problems += output_mode_problems(settings.get("models.answer_services"))' in preflight


# Contact details (decided 2026-10-15) ---------------------------------------------------------------------
HR = "Benefits help. Contact Jane Doe at jane.doe@benefitsco.com or (555) 201-7788. Forms are online."
OTHER = "Payroll questions go to Sam Lee at sam.lee@payrollco.com."
CORPORATE = {"email_domains": ["corp.com"], "phone_numbers": ["+1 800 555 0100"]}


@pytest.mark.parametrize("text, lines, kinds", [
    ('Contact "Jane Doe" at "jane.doe@benefitsco.com" or 555.201.7788.', [2], []),        # in the cited lines
    ("Email hr@corp.com or call 1-800-555-0100.", [1], []),                               # the company's own
    ("Email sam.lee@payrollco.com for help.", [2], ["contact_not_in_evidence"]),           # in another source only
    ("Email jane.smith@gmail.com or call 555-999-1234.", [2], ["contact_not_in_evidence"]),   # in no document
])
def test_emails_and_phone_numbers_must_come_from_the_cited_lines_or_be_the_companys(text, lines, kinds):
    r = contract.check(json.loads(reply((text, [{"source": 1, "lines": lines}]))), [HR, OTHER], SYSTEM,
                       contacts=CORPORATE)
    assert r.kinds == kinds
    if kinds and "payrollco" in text:
        assert "it is in SOURCE [2]: cite that line, or leave it out." in r.problems[0]
    if kinds and "gmail" in text:
        assert len(r.problems) == 2 and "or anywhere in the documents: leave it out." in r.problems[0]


def test_without_a_company_list_only_document_contacts_are_allowed():
    r = contract.check(json.loads(reply(("Email hr@corp.com.", [{"source": 1, "lines": [1]}]))), [HR], SYSTEM)
    assert r.kinds == ["contact_not_in_evidence"]                          # fail closed until it is configured


def test_names_are_held_to_the_quotation_rule():
    from factory.answering import SYSTEM_PROMPT

    assert ("Give a\n   person's name, email address or phone number only as a quote from a line the same block "
            "cites.") in SYSTEM_PROMPT
    invented = reply(('Contact "John Smith" in Benefits.', [{"source": 1, "lines": [2]}]))
    assert checked(invented, [HR]).kinds == ["quote_not_in_evidence"]
