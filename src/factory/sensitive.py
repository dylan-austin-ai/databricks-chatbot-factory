"""Sensitive-data scan at ingestion (ING-8, GOV-9).

Restricted data (government IDs, payment cards) blocks a document before it is
indexed. Other personal data (emails, phone numbers) is flagged for the creator.
The guardrailed model endpoint also masks PII at answer time (GRD-2).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

SSN = re.compile(r"\b(?!000|666|9\d\d)\d{3}-(?!00)\d{2}-(?!0000)\d{4}\b")
CARD = re.compile(r"\b(?:\d[ -]?){13,19}\b")
IBAN = re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b")
EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
PHONE = re.compile(r"(?<!\d)(?:\+?1[ .-]?)?\(?\d{3}\)?[ .-]?\d{3}[ .-]?\d{4}(?!\d)")


def luhn_ok(digits: str) -> bool:
    nums = [int(c) for c in digits][::-1]
    total = sum(nums[0::2]) + sum(sum(divmod(2 * d, 10)) for d in nums[1::2])
    return total % 10 == 0


@dataclass
class ScanResult:
    restricted: dict[str, int] = field(default_factory=dict)
    personal: dict[str, int] = field(default_factory=dict)

    @property
    def blocked(self) -> bool:
        return bool(self.restricted)

    def messages(self) -> list[str]:
        msgs = []
        if self.restricted:
            found = ", ".join(f"{n} {k}" for k, n in self.restricted.items())
            msgs.append(f"Blocked: this document appears to contain restricted data ({found}). "
                        "Remove it and upload a clean copy.")
        if self.personal:
            found = ", ".join(f"{n} {k}" for k, n in self.personal.items())
            msgs.append(f"Contains personal information ({found}). Make sure that's intended.")
        return msgs


def scan(text: str) -> ScanResult:
    r = ScanResult()
    ssn = len(SSN.findall(text))
    cards = sum(1 for m in CARD.findall(text)
                if 13 <= len(d := re.sub(r"\D", "", m)) <= 19 and luhn_ok(d))
    ibans = len(IBAN.findall(text))
    for k, n in (("Social Security numbers", ssn), ("payment card numbers", cards),
                 ("bank account numbers", ibans)):
        if n:
            r.restricted[k] = n
    for k, n in (("email addresses", len(EMAIL.findall(text))),
                 ("phone numbers", len(PHONE.findall(text)))):
        if n:
            r.personal[k] = n
    return r


def redact(text: str) -> tuple[str, list[str]]:
    """Mask restricted numbers in model output (GRD-007): SSNs, payment cards, IBANs."""
    found = []

    def _card(m: re.Match) -> str:
        d = re.sub(r"\D", "", m.group(0))
        if 13 <= len(d) <= 19 and luhn_ok(d):
            found.append("payment card number")
            return "[redacted card number]"
        return m.group(0)

    out = SSN.sub(lambda m: (found.append("Social Security number"), "[redacted SSN]")[1], text or "")
    out = CARD.sub(_card, out)
    out = IBAN.sub(lambda m: (found.append("bank account number"), "[redacted account number]")[1], out)
    return out, found

