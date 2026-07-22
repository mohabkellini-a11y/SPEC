"""Scoring weights + digest/CSV output."""

from __future__ import annotations

from datetime import date, timedelta

from sqlmodel import Session

from eversafe_leads import db as dbmod
from eversafe_leads.models import Party, PartyRole, Permit, Signal
from eversafe_leads.reports import build_digest, export_permits_csv
from eversafe_leads.scoring import Scorer

SCORING = {
    "signals": {
        "fire_review_rejection_14d": 40,
        "second_plus_fire_review_cycle": 15,
        "target_list_a": 30,
        "target_list_b": 20,
        "high_value_niche": 25,
        "valuation_over_2m": 15,
        "valuation_500k_2m": 8,
        "recently_contacted_30d": -50,
    },
    "recency": {"points": 10, "full_days": 30, "zero_days": 180},
    "high_value_niche_keywords": ["data center", "assisted living", "clean agent"],
}


def _permit(**kw) -> Permit:
    base = dict(jurisdiction_id=1, record_number="X", source_url="u")
    base.update(kw)
    return Permit(**base)


def test_valuation_and_recency():
    scorer = Scorer(scoring=SCORING)
    today = date(2026, 7, 22)
    p = _permit(valuation=2_500_000, applied_date=today)
    sl = scorer.score_permit(p, today=today)
    assert sl.breakdown["valuation_over_2m"] == 15
    assert sl.breakdown["permit_recency"] == 10
    assert sl.score == 25


def test_recency_decay_to_zero():
    scorer = Scorer(scoring=SCORING)
    today = date(2026, 7, 22)
    # 180+ days old -> no recency points
    p = _permit(applied_date=today - timedelta(days=200))
    sl = scorer.score_permit(p, today=today)
    assert "permit_recency" not in sl.breakdown


def test_niche_and_signal_stacking():
    scorer = Scorer(scoring=SCORING)
    today = date(2026, 7, 22)
    p = _permit(
        description="New DATA CENTER clean agent suppression", valuation=800_000, applied_date=today
    )
    signals = [
        Signal(permit_id=1, kind="fire_review_rejection", dedup_key="a"),
        Signal(permit_id=1, kind="second_fire_cycle", dedup_key="b"),
    ]
    sl = scorer.score_permit(p, signals=signals, today=today)
    assert sl.breakdown["fire_review_rejection_14d"] == 40
    assert sl.breakdown["second_plus_fire_review_cycle"] == 15
    assert sl.breakdown["high_value_niche"] == 25
    assert sl.breakdown["valuation_500k_2m"] == 8
    assert sl.score == 40 + 15 + 25 + 8 + 10


def test_digest_and_csv_render():
    engine = dbmod.get_engine(":memory:")
    dbmod.init_db(engine)
    today = date(2026, 7, 22)
    with Session(engine) as session:
        juris = dbmod.get_or_create_jurisdiction(
            session,
            slug="orlando",
            display_name="City of Orlando",
            platform="socrata",
            base_url="https://x",
        )
        p = Permit(
            jurisdiction_id=juris.id,
            record_number="FIR2026-1",
            source_url="u",
            record_type="Fire Permit",
            record_subtype="FireSupp",
            status="Open",
            valuation=1_200_000,
            address_line1="123 MAIN ST",
            applied_date=today,
        )
        session.add(p)
        session.commit()
        session.refresh(p)
        session.add(
            Party(
                permit_id=p.id,
                role=PartyRole.contractor,
                raw_name="WAYNE AUTOMATIC FIRE SPRINKLERS INC",
                phone="(407)656-3030",
            )
        )
        session.add(
            Signal(
                permit_id=p.id,
                kind="fire_review_rejection",
                cycle_number=2,
                department="Fire",
                status="Corrections Required",
                comment_text="Provide NFPA 13 hydraulic calcs.",
                dedup_key="k1",
            )
        )
        session.commit()

        md = build_digest(session, today=today)
        assert "HOT fire-review signals (1)" in md
        assert "FIR2026-1" in md
        assert "NFPA 13" in md
        assert "WAYNE AUTOMATIC" in md
        assert "Suggested opener:" in md

        csv_text = export_permits_csv(session, today=today)
        assert "record_number" in csv_text.splitlines()[0]
        assert "FIR2026-1" in csv_text
        assert "WAYNE AUTOMATIC FIRE SPRINKLERS INC" in csv_text
