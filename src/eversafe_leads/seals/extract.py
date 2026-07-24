"""Seal text extraction (Module 4, step 5).

Pure text → seal-candidate logic, independent of how the text was obtained
(digital signature, PDF text layer, or OCR). The regexes below implement SPEC §6:
PE license numbers in several notations, CA numbers, plus surrounding context.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# License-number notations (SPEC §6). Ordered by specificity.
_PE_PATTERNS = [
    re.compile(r"\bFL\s*(?:PE|P\.E\.)\s*#?\s*(\d{4,6})\b", re.I),
    re.compile(r"\b(?:P\.?E\.?)\s*#\s*(\d{4,6})\b", re.I),
    re.compile(r"\bLICENSE\s*(?:NO\.?|NUMBER|#)\s*(\d{4,6})\b", re.I),
    re.compile(r"\b(?:REGISTRATION|REG\.?)\s*(?:NO\.?|#)?\s*(\d{4,6})\b", re.I),
]
_CA_PATTERN = re.compile(
    r"\b(?:CA|C\.A\.|CERTIFICATE\s+OF\s+AUTHORIZATION)\s*#?\s*(\d{3,6})\b", re.I
)
# Engineer name near a "P.E." marker: "JOHN A. SMITH, P.E."
_NAME_PE = re.compile(r"([A-Z][A-Za-z.'-]+(?:\s+[A-Z][A-Za-z.'-]*){1,3})\s*,?\s*P\.?\s*E\.?\b")
_DISCIPLINE = re.compile(
    r"\b(FIRE\s*PROTECTION|MECHANICAL|ELECTRICAL|CIVIL|STRUCTURAL|PLUMBING)\b", re.I
)

_CONTEXT = 200


@dataclass
class SealCandidate:
    license_number: str | None = None  # normalized, e.g. "PE54321"
    engineer_name: str | None = None
    discipline: str | None = None
    ca_number: str | None = None
    raw_text: str = ""
    reasons: list[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not (self.license_number or self.engineer_name)


def _context_around(text: str, start: int, end: int) -> str:
    lo = max(0, start - _CONTEXT // 2)
    hi = min(len(text), end + _CONTEXT // 2)
    return re.sub(r"\s+", " ", text[lo:hi]).strip()


def extract_seal(text: str) -> SealCandidate:
    """Return the best seal candidate found in ``text`` (may be empty)."""
    cand = SealCandidate()
    if not text:
        return cand

    for pat in _PE_PATTERNS:
        m = pat.search(text)
        if m:
            cand.license_number = f"PE{m.group(1)}"
            cand.raw_text = _context_around(text, m.start(), m.end())
            cand.reasons.append(f"pe:{pat.pattern[:18]}")
            break

    m = _NAME_PE.search(text)
    if m:
        cand.engineer_name = re.sub(r"\s+", " ", m.group(1)).strip().rstrip(",")
        cand.reasons.append("name:P.E.")

    m = _CA_PATTERN.search(text)
    if m:
        cand.ca_number = f"CA{m.group(1)}"
        cand.reasons.append("ca")

    m = _DISCIPLINE.search(text)
    if m:
        cand.discipline = re.sub(r"\s+", " ", m.group(1)).strip().title()

    if not cand.raw_text:
        cand.raw_text = re.sub(r"\s+", " ", text[:_CONTEXT]).strip()
    return cand
