"""Custom guardrails that run inside the shared agent (GRD-9..15, GRD-3/4/11).

Org-wide built-ins (safety, PII masking, jailbreak/prompt injection, rate
limits) run on the guardrailed AI Gateway endpoint the agent calls (GRD-1/2/5/6/7).
Per-bot rules run here because endpoint guardrails apply to every bot equally.

Everything in this module is pure Python so it is unit-testable.
"""
from __future__ import annotations

import re

URL_RE = re.compile(r"https?://[^\s)\]>\"']+")
CITE_RE = re.compile(r"\[(\d+)\]")

LEAK_MARKERS = ["You are the", "SOURCES:", "Rules you must follow", "<<<SOURCE"]



def norm(text: str) -> str:
    """Normalize for quote matching: curly quotes, dashes, whitespace, case."""
    text = (text or "").replace("’", "'").replace("‘", "'")
    text = text.replace("“", '"').replace("”", '"').replace("–", "-").replace("—", "-")
    return re.sub(r"\s+", " ", text).strip().lower()


# Prompt injection in retrieved text (DCL-8 at ingestion, ANS-5 at runtime) -------------
INJECTION_PATTERNS = [
    r"ignore (all |any |the )?(previous|prior|above|earlier) (instructions|rules|directions)",
    r"disregard (all |any |the )?(previous|prior|above|earlier|system) (instructions|rules|prompt)",
    r"forget (all |any |your )?(previous|prior) (instructions|rules)",
    r"you are now (a|an|in) ",
    r"(reveal|print|show|repeat) (your|the) (system prompt|instructions|hidden prompt)",
    r"new (system )?instructions?:",
    r"\bsystem prompt\b",
    r"<\|?(im_start|im_end|system)\|?>",
    r"\[/?(inst|sys)\]",
    r"act as (an? )?(unrestricted|jailbroken|developer mode)",
    r"do anything now",
]
_INJECTION_RE = re.compile("|".join(f"(?:{p})" for p in INJECTION_PATTERNS), re.I)


def find_injection(text: str) -> list[str]:
    """Instruction-like phrases aimed at an AI model."""
    return [m.group(0) for m in _INJECTION_RE.finditer(text or "")]


def strip_injection(text: str) -> tuple[str, list[str]]:
    """Remove the sentence containing each injection phrase before the model sees it."""
    hits = find_injection(text)
    if not hits:
        return text, []
    sentences = re.split(r"(?<=[.!?\n])\s+", text)
    kept = [s for s in sentences if not _INJECTION_RE.search(s)]
    return " ".join(kept) + " [removed: text that looked like instructions to an AI]", hits


# Output checks --------------------------------------------------------------
def excerpt_found(excerpt: str, source: str) -> bool:
    """Whether an excerpt is an exact piece of a source (case, spacing and curly quotes aside).
    The agent and the quality check both use this, so they can't disagree about an excerpt."""
    found = norm(excerpt).rstrip(".,;:")
    return bool(found) and found in norm(source)


# A sensitive-data policy that redacts puts a label like [UK_NHS] or [EMAIL_ADDRESS] in place of what
# it found. Such a label in the model's reply that no source contains came from the gateway.
PLACEHOLDER_RE = re.compile(r"\[[A-Z]{2,}(?:_[A-Z0-9]+)*\]")


def gateway_placeholders(text: str, sources: list[str]) -> list[str]:
    """Redaction labels in `text` that aren't in any source: the gateway changed the text."""
    corpus = " ".join(sources)
    return sorted({t for t in PLACEHOLDER_RE.findall(text or "") if t not in corpus})


def invented_links(answer: str, sources: list[str]) -> list[str]:
    """GRD-12: only URLs that appear in source docs."""
    allowed = set(URL_RE.findall(" ".join(sources)))
    return [u for u in URL_RE.findall(answer) if u.rstrip(".,") not in allowed]


def has_citation(answer: str) -> bool:
    return bool(CITE_RE.search(answer))


def leaks_prompt(answer: str, system_prompt: str) -> bool:
    """GRD-8: output echoes the system prompt or internal markers."""
    a = norm(answer)
    if any(norm(m) in a for m in LEAK_MARKERS):
        return True
    sp = norm(system_prompt)
    # any 12-word window of the system prompt appearing verbatim
    words = sp.split()
    for i in range(0, max(0, len(words) - 12), 6):
        if " ".join(words[i:i + 12]) in a:
            return True
    return False


def platform_rules(rules: dict[str, str], disabled: list[str]) -> list[str]:
    """Rules every chatbot follows unless an admin turned one off for it."""
    return [text for key, text in (rules or {}).items() if key not in set(disabled or [])]
