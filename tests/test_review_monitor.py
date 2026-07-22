"""Phase 3 acceptance: two snapshots of a permit -> exactly one HOT signal."""

from __future__ import annotations

from datetime import date, timedelta

from sqlmodel import Session

from eversafe_leads import db as dbmod
from eversafe_leads.models import PermitDetail, Review, ReviewStub
from eversafe_leads.review_monitor import ReviewMonitor

SCORING = {
    "fire_review_departments": ["fire", "life safety", "fire marshal", "fpb"],
    "hot_review_statuses": [
        "reject",
        "corrections",
        "comments",
        "revise",
        "resubmit",
        "disapprov",
        "incomplete",
        "not approved",
    ],
}


def _review(**kw) -> Review:
    base = dict(permit_id=1, cycle_number=1, department="Fire", status="In Review")
    base.update(kw)
    return Review(**base)


def test_clean_review_is_not_hot():
    mon = ReviewMonitor(scoring=SCORING)
    signals = mon.evaluate([_review(status="In Review")])
    assert signals == []


def test_fire_corrections_fires_one_signal_with_comment():
    mon = ReviewMonitor(scoring=SCORING)
    reviews = [
        _review(
            cycle_number=1,
            department="Fire Plans Review",
            status="Corrections Required",
            comment_text="Provide hydraulic calcs per NFPA 13; riser detail missing.",
        )
    ]
    signals = mon.evaluate(reviews)
    hot = [s for s in signals if s.kind == "fire_review_rejection"]
    assert len(hot) == 1
    assert hot[0].cycle_number == 1
    assert "NFPA 13" in hot[0].comment_text


def test_non_fire_department_rejection_is_ignored():
    mon = ReviewMonitor(scoring=SCORING)
    reviews = [_review(department="Zoning", status="Corrections Required")]
    assert mon.evaluate(reviews) == []


def test_second_cycle_signal():
    mon = ReviewMonitor(scoring=SCORING)
    reviews = [
        _review(cycle_number=1, status="Approved"),
        _review(cycle_number=2, status="In Review"),
    ]
    kinds = {s.kind for s in mon.evaluate(reviews)}
    assert "second_fire_cycle" in kinds


def test_stuck_in_fire_review():
    mon = ReviewMonitor(scoring=SCORING, stuck_days=21)
    old = date(2026, 6, 1)
    reviews = [_review(status="In Review", status_date=old)]
    signals = mon.evaluate(reviews, today=old + timedelta(days=30))
    assert any(s.kind == "stuck_in_fire_review" for s in signals)


def test_two_snapshots_dedup_to_one_signal():
    """The acceptance case: re-evaluating the same worsened snapshot twice
    persists exactly one HOT signal."""
    engine = dbmod.get_engine(":memory:")
    dbmod.init_db(engine)
    mon = ReviewMonitor(scoring=SCORING)

    with Session(engine) as session:
        juris = dbmod.get_or_create_jurisdiction(
            session,
            slug="demo",
            display_name="Demo",
            platform="accela",
            base_url="https://example",
        )
        # Snapshot 1: fire review cycle 1, clean.
        detail_v1 = PermitDetail(
            record_number="PRM-1",
            source_url="https://example/PRM-1",
            reviews=[ReviewStub(cycle_number=1, department="Fire", status="In Review")],
        )
        permit, _ = dbmod.persist_detail(session, juris.id, detail_v1)
        assert mon.detect_new(session, permit.id) == []  # nothing hot yet

        # Snapshot 2: same cycle now returns corrections with a comment.
        detail_v2 = PermitDetail(
            record_number="PRM-1",
            source_url="https://example/PRM-1",
            reviews=[
                ReviewStub(
                    cycle_number=1,
                    department="Fire",
                    status="Corrections Required",
                    comment_text="Sprinkler coverage gap in Room 104.",
                )
            ],
        )
        dbmod.persist_detail(session, juris.id, detail_v2)

        first = mon.detect_new(session, permit.id)
        assert len(first) == 1
        assert first[0].kind == "fire_review_rejection"
        assert first[0].cycle_number == 1
        assert "Room 104" in first[0].comment_text

        # Running again fires nothing new (dedup: one alert per permit per cycle).
        second = mon.detect_new(session, permit.id)
        assert second == []
