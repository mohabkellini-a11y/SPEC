"""Entity resolution across sources (SPEC §6, Module 3).

Match order (never auto-merge below the fuzzy bar):

1. **exact license** — a shared license number is definitive.
2. **exact normalized name** — identical normalized keys.
3. **fuzzy_auto** — ``token_set_ratio >= 92`` AND a corroborating address or
   phone match.
4. **review_queue** — ``85 <= ratio < 92``: surfaced for a human, not merged.
5. **new** — everything else becomes a new company.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from rapidfuzz import fuzz

from .normalize import digits_only, normalize_company

AUTO_THRESHOLD = 92
REVIEW_THRESHOLD = 85


class CompanyLike(Protocol):
    id: int | None
    normalized_key: str
    license_numbers: list[str]
    phone: str | None
    address: str | None


@dataclass
class Candidate:
    name: str
    license_number: str | None = None
    phone: str | None = None
    address: str | None = None

    @property
    def normalized_key(self) -> str:
        return normalize_company(self.name)


@dataclass
class MatchResult:
    action: str  # exact_license | exact_name | fuzzy_auto | review_queue | new
    company: CompanyLike | None = None
    score: float = 0.0
    reasons: list[str] = field(default_factory=list)


def _zip5(address: str | None) -> str:
    if not address:
        return ""
    digits = digits_only(address)
    # last 5 digits are usually the ZIP
    return digits[-5:] if len(digits) >= 5 else ""


def _address_match(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    if _zip5(a) and _zip5(a) == _zip5(b):
        return True
    return fuzz.token_set_ratio(a.upper(), b.upper()) >= 90


def _phone_match(a: str | None, b: str | None) -> bool:
    da, db = digits_only(a), digits_only(b)
    return bool(da) and da == db


def resolve(candidate: Candidate, companies: list[CompanyLike]) -> MatchResult:
    key = candidate.normalized_key
    cand_lic = (candidate.license_number or "").strip().upper()

    # 1) exact license
    if cand_lic:
        for c in companies:
            if any(cand_lic == (lic or "").strip().upper() for lic in (c.license_numbers or [])):
                return MatchResult("exact_license", c, 100.0, [f"license {cand_lic}"])

    # 2) exact normalized name
    if key:
        for c in companies:
            if c.normalized_key and c.normalized_key == key:
                return MatchResult("exact_name", c, 100.0, [f"normalized name '{key}'"])

    # 3/4) fuzzy
    best: CompanyLike | None = None
    best_score = 0.0
    for c in companies:
        if not c.normalized_key:
            continue
        score = fuzz.token_set_ratio(key, c.normalized_key)
        if score > best_score:
            best_score, best = score, c

    if best is not None and best_score >= AUTO_THRESHOLD:
        if _address_match(candidate.address, best.address) or _phone_match(
            candidate.phone, best.phone
        ):
            return MatchResult(
                "fuzzy_auto", best, best_score, [f"fuzzy {best_score:.0f} + address/phone"]
            )
        # High name similarity but no corroboration -> queue, don't merge.
        return MatchResult(
            "review_queue", best, best_score, [f"fuzzy {best_score:.0f}, no corrob."]
        )

    if best is not None and best_score >= REVIEW_THRESHOLD:
        return MatchResult("review_queue", best, best_score, [f"fuzzy {best_score:.0f}"])

    return MatchResult("new", None, best_score, ["no match above threshold"])
