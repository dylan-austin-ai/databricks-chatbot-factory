from factory import guardrails as g

SOURCES = [
    "Claims over $10,000 require supervisor approval before payment.",
    "Water damage from floods is excluded. See https://intranet.corp.com/flood for details.",
]
SYSTEM = "You are the \"Claims\" assistant. Rules you must follow: answer only from sources " * 2


def test_exact_quote_passes_with_whitespace_and_curly_quotes():
    ans = 'Per the Claims Manual, page 3, “Claims over $10,000   require supervisor approval” [1].'
    assert g.verify_quotes(ans, SOURCES) == []
    assert g.check_answer(ans, SOURCES, SYSTEM, False, 60).ok


def test_invented_quote_fails():
    ans = 'The manual says "claims over $5,000 need two approvals" [1].'
    r = g.check_answer(ans, SOURCES, SYSTEM, False, 60)
    assert not r.ok and "quote_check" in r.fired


def test_invented_link_fails_real_link_passes():
    assert g.invented_links("See https://intranet.corp.com/flood.", SOURCES) == []
    assert g.invented_links("See https://evil.example/x", SOURCES) == ["https://evil.example/x"]


def test_citation_range():
    assert g.invalid_citations("A [1] B [3]", 2) == [3]
    assert g.has_citation("text [2]")


def test_prompt_leak_detected():
    assert g.leaks_prompt("Sure! Rules you must follow: answer only from sources", SYSTEM)
    assert not g.leaks_prompt('Flood damage is excluded [2].', SYSTEM)


def test_verbatim_limit_caps_long_quotes():
    long = '"' + " ".join(["word"] * 100) + '" [1]'
    text, changed = g.cap_quotes(long, 10)
    assert changed and text.count("word") == 10


def test_platform_rules_admin_can_disable(settings):
    rules = settings.get("guardrails.platform_rules")
    assert len(g.platform_rules(rules, [])) == len(rules) >= 11
    kept = g.platform_rules(rules, ["coverage_advice"])
    assert len(kept) == len(rules) - 1 and rules["coverage_advice"] not in kept
