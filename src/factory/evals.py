"""Evaluation and the combined go-live gate (EVG-1..5, EVL-4..9, MON-6, CAS-4).

Gate (EVG-1), all must hold:
  * retrieval: Hit@1 >= 0.80, Hit@3 >= 0.90, Hit@5 >= 0.95, MRR >= 0.85 (right document;
    right page reported alongside, EVG-2)
  * judged correctness, groundedness, relevance, refusal and citation metrics >= 90%
    (Databricks built-in judges via mlflow.genai.evaluate, see jobs/run_evals.py)
  * zero deterministic failures: citation mapping, channel/expiry leakage, sensitive output
  * LLM-judged safety failures don't fail automatically: they go to the Reviewer, who can
    override with a logged justification (EVG-3)
"""
from __future__ import annotations

import random
from dataclasses import dataclass

from . import guardrails as g
from . import sensitive

REFUSAL_OUTCOMES = {"refused", "no_source"}
RETRIEVAL_KS = (1, 3, 5)

def call_agent(client, endpoint: str, bot_id: str, question: str, channel: str = "candidate",
               synthetic: bool = True) -> tuple[str, dict]:
    resp = client.responses.create(
        model=endpoint, input=[{"role": "user", "content": question}],
        extra_body={"custom_inputs": {"bot_id": bot_id, "channel": channel, "synthetic": synthetic}})
    text = getattr(resp, "output_text", None) or ""
    if not text:
        for item in resp.output or []:
            for c in getattr(item, "content", []) or []:
                text += getattr(c, "text", "") or ""
    custom = getattr(resp, "custom_outputs", None) or (resp.model_extra or {}).get("custom_outputs", {})
    return text, custom or {}


# Retrieval metrics (EVG-1, EVG-2) ---------------------------------------------------------
def _pages(value) -> set[int]:
    if isinstance(value, str):
        return {int(x) for x in value.replace(" ", "").split(",") if x.isdigit()}
    return {int(x) for x in (value or [])}


def retrieval_metrics(retrieval: list[dict], expected_doc: str | None,
                      expected_pages=None) -> dict[str, float]:
    """retrieval: ranked [{rank, doc_id, pages ('3, 4' 1-based)}]. Doc-level and page-level."""
    if not expected_doc:
        return {}
    exp_pages = _pages(expected_pages)
    doc_rank = page_rank = None
    for r in sorted(retrieval, key=lambda x: x.get("rank") or 0):
        if r.get("doc_id") != expected_doc:
            continue
        doc_rank = doc_rank or r["rank"]
        if exp_pages and page_rank is None and _pages(r.get("pages")) & exp_pages:
            page_rank = r["rank"]
    out = {f"hit_at_{k}": float(bool(doc_rank and doc_rank <= k)) for k in RETRIEVAL_KS}
    out["mrr"] = 1.0 / doc_rank if doc_rank else 0.0
    if exp_pages:
        out.update({f"page_hit_at_{k}": float(bool(page_rank and page_rank <= k)) for k in RETRIEVAL_KS})
        out["page_mrr"] = 1.0 / page_rank if page_rank else 0.0
    return out


# Deterministic checks (zero tolerance), run as MLflow code scorers ------------------------
def citation_check(answer: str, custom: dict, chunk_texts: dict[str, str]) -> tuple[bool, list[str]]:
    """Every citation maps to a retrieved chunk, and its excerpt is verbatim from it (ANS-1)."""
    problems = []
    retrieved = set(custom.get("retrieved_chunk_ids") or [])
    cites = custom.get("citations") or []
    if not cites or not g.has_citation(answer):
        problems.append("answer has no citations")
    for c in cites:
        if c.get("chunk_id") not in retrieved:
            problems.append(f"citation [{c.get('n')}] is not a retrieved chunk")
        text = chunk_texts.get(c.get("chunk_id"), "")
        if text and g.norm(c.get("excerpt", "")).rstrip(".,;:") not in g.norm(text):
            problems.append(f"excerpt for [{c.get('n')}] is not verbatim")
    bad_quotes = g.verify_quotes(answer, [chunk_texts.get(c.get("chunk_id"), "") for c in cites])
    if bad_quotes:
        problems.append("quoted text not found in cited sources")
    return not problems, problems


def leakage_check(custom: dict, allowed_chunk_ids: set[str]) -> list[str]:
    """Nothing outside the channel's current, unexpired set may be retrieved (EVG-1)."""
    return [c for c in (custom.get("retrieved_chunk_ids") or []) if c not in allowed_chunk_ids]


def aggregate(rows: list[dict[str, float]]) -> dict[str, float]:
    """Mean of each score across cases (cases without a score don't count)."""
    totals: dict[str, list[float]] = {}
    for r in rows:
        for k, v in r.items():
            if v is not None:
                totals.setdefault(k, []).append(float(v))
    return {k: sum(v) / len(v) for k, v in totals.items() if v}


@dataclass
class GateResult:
    passed: bool
    failures: list[str]
    needs_safety_review: bool


def gate(metrics: dict[str, float], thresholds: dict[str, float], zero_failures: dict[str, int],
         zero_checks: list[str], n_cases: int, min_cases: int,
         safety_override: bool = False, required: tuple | list = (), unscored: int = 0) -> GateResult:
    """`required`: metrics that must be present (enabled judges, expected checks). A judge that
    errored or never ran fails the gate instead of being skipped."""
    failures = []
    if n_cases < min_cases:
        failures.append(f"Only {n_cases} approved test question(s); at least {min_cases} are required.")
    if unscored:
        failures.append(f"{unscored} test question(s) weren't scored; re-run the check.")
    for name in required:
        if name not in metrics:
            failures.append(f"{name.replace('_', ' ')} wasn't scored (judge unavailable?); re-run the check.")
    for check in zero_checks:
        n = zero_failures.get(check, 0)
        if n:
            failures.append(f"{n} {check.replace('_', ' ')} failure(s); this must be zero.")
    needs_review = False
    for name, threshold in thresholds.items():
        if name not in metrics or metrics[name] >= threshold:
            continue
        if name == "safety":
            needs_review = not safety_override
            if needs_review:
                failures.append("Some answers were flagged as possibly unsafe and need Reviewer review.")
            continue
        failures.append(f"{name.replace('_', ' ')} is {metrics[name]:.0%}, needs {threshold:.0%}.")
    return GateResult(not failures, failures, needs_review)


def sample_for_judging(rows: list[dict], critical_bots: set[str], rate: float,
                       seed: int | None = None) -> list[dict]:
    """MON-6: judge `rate` of answers; critical bots are judged at 100%."""
    rnd = random.Random(seed)
    return [r for r in rows if r["bot_id"] in critical_bots or rnd.random() < rate]


def citation_accuracy(response: str, cited_texts: list[str]) -> tuple[bool, str]:
    """Production judging variant (no structured citations available in the log table)."""
    if not g.has_citation(response):
        return False, "No source citation in the answer."
    bad = g.verify_quotes(response, cited_texts)
    if bad:
        return False, f"Quote not found in cited sources: {bad[0][:80]}"
    return True, "Citations and quotes verified."


ZERO_CHECKS = ("citation_mapping", "leakage", "sensitive_output")


def deterministic_scores(kind: str, outputs: dict, expectations: dict, chunk_texts: dict[str, str],
                         allowed_chunk_ids: set[str]) -> dict[str, float]:
    """Code-scorer results for one case: retrieval Hit@k/MRR, refusal accuracy, citation accuracy,
    and pass(1)/fail(0) for each zero-tolerance check."""
    custom = outputs.get("custom") or {}
    answer = custom.get("answer") or outputs.get("response", "")
    outcome = custom.get("outcome", "unknown")
    out = {"leakage": float(not leakage_check(custom, allowed_chunk_ids)),
           "sensitive_output": float(not sensitive.scan(outputs.get("response", "")).blocked)}
    if kind == "out_of_scope":
        out["refusal_accuracy"] = float(outcome in REFUSAL_OUTCOMES)
        return out
    out.update(retrieval_metrics(custom.get("retrieval") or [], expectations.get("source_doc_id"),
                                 expectations.get("expected_pages")))
    if outcome == "answered":
        ok, _ = citation_check(answer, custom, chunk_texts)
        out["citation_mapping"] = out["citation_accuracy"] = float(ok)
    else:
        out["citation_accuracy"] = 0.0
    return out


def zero_failures(rows: list[dict[str, float]]) -> dict[str, int]:
    return {k: sum(1 for r in rows if r.get(k) == 0.0) for k in ZERO_CHECKS}


def dataset_record(case: dict) -> dict:
    """Golden-set row -> MLflow evaluation dataset record (inputs + expectations)."""
    exp = {"kind": case["kind"], "eval_id": case["eval_id"]}
    if case.get("expected_answer"):
        exp["expected_response"] = case["expected_answer"]
    if case.get("source_doc_id"):
        exp["source_doc_id"] = case["source_doc_id"]
    if case.get("expected_pages"):
        exp["expected_pages"] = list(case["expected_pages"])
    if case["kind"] == "out_of_scope":
        exp["guidelines"] = ["The response declines to answer and points the user to the chatbot owner."]
    return {"inputs": {"question": case["question"]}, "expectations": exp}
