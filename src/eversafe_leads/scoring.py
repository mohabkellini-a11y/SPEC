"""Lead scoring (SPEC §7).

Weights live in ``config/scoring.yaml`` and are editable without touching code.
Every score returns a ``score_breakdown`` dict that explains itself — each entry
is ``{reason: points}`` so a human can see exactly why a lead scored what it did.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from .config import load_scoring
from .models import Company, Permit, Signal


@dataclass
class ScoredLead:
    permit: Permit
    score: float
    breakdown: dict[str, float] = field(default_factory=dict)


class Scorer:
    def __init__(self, scoring: dict | None = None) -> None:
        cfg = scoring or load_scoring()
        self.signals = cfg.get("signals", {})
        self.recency = cfg.get("recency", {"points": 10, "full_days": 30, "zero_days": 180})
        self.niche_keywords = [k.lower() for k in cfg.get("high_value_niche_keywords", [])]
        # Word-boundary patterns so short tokens like "ess" (energy storage) do
        # not substring-match inside "wellness"/"business"/"access".
        self._niche_patterns = [
            re.compile(rf"(?<![a-z0-9]){re.escape(k)}(?![a-z0-9])") for k in self.niche_keywords
        ]

    def _recency_points(self, applied: date | None, today: date) -> float:
        if not applied:
            return 0.0
        days = (today - applied).days
        full = self.recency["full_days"]
        zero = self.recency["zero_days"]
        pts = self.recency["points"]
        if days <= full:
            return float(pts)
        if days >= zero:
            return 0.0
        # linear decay between full and zero
        frac = (zero - days) / (zero - full)
        return round(pts * frac, 2)

    def _matches_niche(self, permit: Permit) -> bool:
        haystack = " ".join(
            filter(
                None,
                [
                    permit.description,
                    permit.occupancy_type,
                    permit.record_type,
                    permit.record_subtype,
                ],
            )
        ).lower()
        if any(p.search(haystack) for p in self._niche_patterns):
            return True
        # A large-footprint project (>100k sf) is high-value on size alone.
        if permit.square_footage and permit.square_footage > 100_000:
            return True
        return False

    def score_permit(
        self,
        permit: Permit,
        signals: list[Signal] | None = None,
        company: Company | None = None,
        today: date | None = None,
        on_target_list: str | None = None,
        recently_contacted: bool = False,
    ) -> ScoredLead:
        today = today or date.today()
        signals = signals or []
        breakdown: dict[str, float] = {}
        w = self.signals

        kinds = {s.kind for s in signals}
        if "fire_review_rejection" in kinds:
            breakdown["fire_review_rejection_14d"] = w.get("fire_review_rejection_14d", 40)
        if "second_fire_cycle" in kinds:
            breakdown["second_plus_fire_review_cycle"] = w.get("second_plus_fire_review_cycle", 15)

        list_choice = on_target_list or (
            "A"
            if company and company.is_fp_contractor and not company.has_fp_pe_on_record
            else None
        )
        if list_choice == "A":
            breakdown["target_list_a"] = w.get("target_list_a", 30)
        elif list_choice == "B":
            breakdown["target_list_b"] = w.get("target_list_b", 20)

        if self._matches_niche(permit):
            breakdown["high_value_niche"] = w.get("high_value_niche", 25)

        val = permit.valuation or 0
        if val > 2_000_000:
            breakdown["valuation_over_2m"] = w.get("valuation_over_2m", 15)
        elif val >= 500_000:
            breakdown["valuation_500k_2m"] = w.get("valuation_500k_2m", 8)

        recency_pts = self._recency_points(permit.applied_date, today)
        if recency_pts:
            breakdown["permit_recency"] = recency_pts

        if recently_contacted:
            breakdown["recently_contacted_30d"] = w.get("recently_contacted_30d", -50)

        total = round(sum(breakdown.values()), 2)
        return ScoredLead(permit=permit, score=total, breakdown=breakdown)
