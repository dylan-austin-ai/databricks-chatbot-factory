"""A quality-check result says how many questions got an answer before any score."""
from factory import evals

ROWS = [{"correctness": 1.0, "groundedness": 1.0}, {"correctness": 0.0, "groundedness": 1.0},
        {"correctness": 0.0, "groundedness": 1.0}, {"correctness": 0.0, "groundedness": 1.0}]
OUTCOMES = ["answered", "answered", "unverified", "blocked"]


def test_the_headline_counts_answers_and_names_what_happened_to_the_rest():
    counts = {"answered": 20, "unverified": 20, "refused": 6, "blocked": 3, "no_source": 1}
    assert evals.answer_headline(counts, 50) == (
        "Answered 20 of 50 test questions. The others: withheld as unverified 20, declined as out of scope 6, "
        "blocked by a guardrail 3, said its documents don't cover it 1.")
    assert evals.answer_headline({"answered": 3}, 3) == "Answered 3 of 3 test questions."


def test_scores_over_the_answers_given_sit_beside_the_gate_and_do_not_replace_it():
    view = evals.answered_view(ROWS, OUTCOMES)
    assert view == {"answered": 2, "asked": 4, "metrics": {"correctness": 0.5, "groundedness": 1.0}}
    assert evals.aggregate(ROWS) == {"correctness": 0.25, "groundedness": 1.0}   # the gate still counts every question


def test_why_answers_were_withheld_is_counted_by_cause():
    said = evals.withheld_summary([["citation_lines"], ["citation_lines", "quote_check"], ["invalid_json"], []])
    assert said == ("named source lines that don't exist, or too many 2, a reply that wasn't valid JSON 1, "
                    "a quote not word for word 1, unknown 1")
