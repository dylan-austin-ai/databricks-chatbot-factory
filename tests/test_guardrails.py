from factory import answer_contract as contract
from factory import guardrails as g

SOURCES = [
    "Claims over $10,000 require supervisor approval before payment.",
    "Water damage from floods is excluded. See https://intranet.corp.com/flood for details.",
]
SYSTEM = "You are the \"Claims\" assistant. Rules you must follow: answer only from sources " * 2


def answered(text, lines=(1,), source=1):
    return {"status": "answered", "blocks": [{"text": text, "evidence": [{"source": source, "lines": list(lines)}]}],
            "conflicts": []}


def test_exact_quote_passes_with_whitespace_and_curly_quotes():
    ans = 'Per the Claims Manual, page 3, “Claims over $10,000   require supervisor approval”.'
    assert contract.check(answered(ans), SOURCES, SYSTEM).ok


def test_invented_quote_fails():
    r = contract.check(answered('The manual says "claims over $5,000 need two approvals".'), SOURCES, SYSTEM)
    assert not r.ok and r.category == contract.EVIDENCE and r.kinds == ["quote_not_in_evidence"]


def test_invented_link_fails_real_link_passes():
    assert g.invented_links("See https://intranet.corp.com/flood.", SOURCES) == []
    assert g.invented_links("See https://evil.example/x", SOURCES) == ["https://evil.example/x"]


def test_citation_range():
    r = contract.check(answered("Approval is needed.", source=3), SOURCES, SYSTEM)
    assert not r.ok and r.kinds == ["citation_lines"] and "There is no SOURCE [3]" in r.problems[0]
    assert g.has_citation("text [2]")


def test_prompt_leak_detected():
    assert g.leaks_prompt("Sure! Rules you must follow: answer only from sources", SYSTEM)
    assert not g.leaks_prompt('Flood damage is excluded [2].', SYSTEM)


def test_verbatim_limit_refuses_long_quotes_instead_of_cutting_them():
    source = [" ".join(f"word{k}" for k in range(100)) + "."]
    long = answered('"' + " ".join(f"word{k}" for k in range(20)) + '"')
    assert contract.check(long, source, SYSTEM).ok                                  # no limit for this chatbot
    r = contract.check(long, source, SYSTEM, limit_quotes=True, max_quote_words=10)
    assert not r.ok and r.kinds == ["quote_too_long"] and "keep each under 10" in r.problems[0]


def test_platform_rules_admin_can_disable(settings):
    rules = settings.get("guardrails.platform_rules")
    assert len(g.platform_rules(rules, [])) == len(rules) >= 11
    kept = g.platform_rules(rules, ["coverage_advice"])
    assert len(kept) == len(rules) - 1 and rules["coverage_advice"] not in kept
