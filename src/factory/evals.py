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


# Running the check without flooding the judge model -------------------------------------------
def check_limits(settings) -> dict[str, str]:
    """The MLflow settings that bound how hard a quality check leans on the judge model.

    Left alone, MLflow scores up to 10 questions at once with up to 10 judges each and waits 300
    seconds for every judge call. A pay-per-token model endpoint answers that by throttling, each
    call then runs into the 300 seconds, and a 50-question check takes hours to report that
    nothing was scored. These keep it to a few calls at a time and give up on a call after a
    minute. Values come from the `quality.check` settings."""
    c = settings.get("quality.check", {}) or {}
    timeout = str(int(c.get("judge_timeout_seconds", 60)))
    return {"MLFLOW_GENAI_EVAL_MAX_WORKERS": str(int(c.get("questions_at_once", 4))),
            "MLFLOW_GENAI_EVAL_MAX_SCORER_WORKERS": str(int(c.get("judges_at_once", 2))),
            "MLFLOW_GENAI_EVAL_SCORER_RATE_LIMIT": str(c.get("judge_calls_per_second", 2)),
            "MLFLOW_GENAI_EVAL_LLM_TIMEOUT": timeout, "MLFLOW_GATEWAY_ROUTE_TIMEOUT_SECONDS": timeout}


def judge_problem(client, endpoint: str, timeout: int = 60) -> str:
    """Ask the judge model one trivial question before a check starts. Returns "" when it
    answers, otherwise one line saying why not (wrong name, no permission, no answer in time),
    so a check that can't be scored stops at once with the reason instead of after every
    question has been asked."""
    try:
        client.chat.completions.create(model=endpoint, max_tokens=5, timeout=timeout,
                                       messages=[{"role": "user", "content": "Reply with the word OK."}])
    except Exception as e:  # noqa: BLE001 - whatever it is, the check can't be scored
        first = next((line.strip() for line in str(e).splitlines() if line.strip()), "")
        return f"{type(e).__name__}: {first}"[:300]
    return ""


def sample_cases(cases: list[dict], limit: int) -> list[dict]:
    """The approved questions a quality check uses when there are more than `limit` of them.

    A chatbot with a hundred documents can have several hundred approved questions; asking and
    judging all of them at every check takes most of an hour. The sample is fixed (the same
    questions every time, so two checks can be compared and a result can't be re-rolled), keeps
    each kind of question in proportion with at least a handful of the rare kinds, and takes
    turns across the source documents so no document dominates. `limit` of 0 means all."""
    import hashlib

    if not limit or len(cases) <= limit:
        return cases

    def stable(text) -> str:
        return hashlib.sha256(str(text).encode()).hexdigest()

    kinds: dict[str, list[dict]] = {}
    for c in cases:
        kinds.setdefault(c.get("kind") or "in_scope", []).append(c)
    quota = {k: max(min(len(v), 5), round(limit * len(v) / len(cases))) for k, v in kinds.items()}
    while sum(quota.values()) > limit:      # the floors can overshoot: trim the largest kind
        biggest = max(quota, key=lambda k: quota[k])
        quota[biggest] -= 1
    chosen = []
    for kind, group in kinds.items():
        by_doc: dict[str, list[dict]] = {}
        for c in sorted(group, key=lambda c: stable(c.get("eval_id"))):
            by_doc.setdefault(str(c.get("source_doc_id") or ""), []).append(c)
        docs = sorted(by_doc, key=stable)
        while len([c for c in chosen if (c.get("kind") or "in_scope") == kind]) < quota[kind]:
            for doc in docs:
                if by_doc[doc] and len([c for c in chosen if (c.get("kind") or "in_scope") == kind]) < quota[kind]:
                    chosen.append(by_doc[doc].pop(0))
    keep = {id(c) for c in chosen}
    return [c for c in cases if id(c) in keep]      # in the order they came


def timed(label: str, work, limit_s: int = 60) -> tuple[bool, object]:
    """Run one step of a diagnostic with a time limit and print how it went:
    "DIAG: <label>: ok in 1.2s" / "FAILED in 0.4s: <reason>" / "STILL WAITING after 60s".
    Returns (it finished without an error, its result). A step that hangs is left behind so the
    diagnostic carries on to the next one."""
    import threading
    import time

    box: dict = {}

    def run() -> None:
        try:
            box["value"] = work()
        except BaseException as e:  # noqa: BLE001 - reported, not raised
            box["error"] = e

    started = time.time()
    thread = threading.Thread(target=run, name=f"diag-{label[:20]}", daemon=True)
    thread.start()
    thread.join(limit_s)
    took = time.time() - started
    if thread.is_alive():
        print(f"DIAG: {label}: STILL WAITING after {limit_s}s (this is where it stalls)", flush=True)
        return False, None
    if "error" in box:
        first = next((line.strip() for line in str(box["error"]).splitlines() if line.strip()), "")
        print(f"DIAG: {label}: FAILED in {took:.1f}s: {type(box['error']).__name__}: {first[:300]}", flush=True)
        return False, None
    print(f"DIAG: {label}: ok in {took:.1f}s", flush=True)
    return True, box.get("value")
