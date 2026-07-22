"""Company / name / address normalization (SPEC §6, Module 3).

The normalized key is what entity resolution matches on. The rule (verbatim from
the SPEC): uppercase, strip punctuation, strip a set of corporate suffixes,
collapse whitespace — while always keeping the original string.
"""

from __future__ import annotations

import re

# Corporate suffixes/qualifiers to strip (SPEC §6). Order-independent; applied
# as whole tokens so we never chop a real word (e.g. "COMPANION" keeps its "CO").
_SUFFIX_TOKENS = [
    "INCORPORATED",
    "INC",
    "LLC",
    "LLP",
    "LLLP",
    "LTD",
    "CORPORATION",
    "CORP",
    "COMPANY",
    "CO",
    "PA",
    "PLLC",
    "PL",
    "GROUP",
    "SERVICES",
    "SERVICE",
    "OF FLORIDA",
    "OF FL",
]
# Longer phrases first so "OF FLORIDA" is removed before a bare "FL"/"OF".
_SUFFIX_TOKENS.sort(key=lambda s: (-len(s.split()), -len(s)))

_DOTS = re.compile(r"\.")
_PUNCT = re.compile(r"[^\w\s]")
_WS = re.compile(r"\s+")


def _depunct(text: str) -> str:
    # Drop periods first so dotted acronyms join up (``L.L.C.`` -> ``LLC``,
    # ``P.A.`` -> ``PA``), then turn any other punctuation into a separator.
    text = _DOTS.sub("", text)
    text = _PUNCT.sub(" ", text)
    return _WS.sub(" ", text).strip()


def normalize_company(name: str | None) -> str:
    """Return the normalized matching key for a company name.

    Empty / ``None`` input yields ``""``. Idempotent.
    """
    if not name:
        return ""
    text = _depunct(name.upper())
    if not text:
        return ""

    # Remove suffix tokens/phrases as whole words, repeatedly (handles
    # "SMITH FIRE SERVICES INC" -> "SMITH FIRE" and trailing stacks).
    changed = True
    while changed:
        changed = False
        for suffix in _SUFFIX_TOKENS:
            pattern = re.compile(rf"(?:^|\s){re.escape(suffix)}(?:\s|$)")
            new = pattern.sub(" ", text).strip()
            new = _WS.sub(" ", new)
            if new != text and new:  # never normalize down to empty
                text = new
                changed = True
    return _WS.sub(" ", text).strip()


def normalize_person(name: str | None) -> str:
    """Normalize a person's name to ``FIRST LAST`` uppercase form.

    Accepts either ``LAST, FIRST M SUFFIX`` (DBPR/records style) or plain
    ``FIRST LAST`` and returns a whitespace-collapsed uppercase key with
    punctuation and common suffixes removed.
    """
    if not name:
        return ""
    text = name.upper().strip()
    if "," in text:
        last, _, first = text.partition(",")
        text = f"{first.strip()} {last.strip()}"
    text = _depunct(text)
    tokens = [t for t in text.split() if t not in {"JR", "SR", "II", "III", "IV"}]
    return " ".join(tokens).strip()


def digits_only(value: str | None) -> str:
    """Phone/ID reduced to digits for comparison (``(407) 656-3030`` ->
    ``4076563030``)."""
    if not value:
        return ""
    return re.sub(r"\D", "", value)
