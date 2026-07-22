"""Contractor->company resolution, permit stats, and target lists."""

from __future__ import annotations

from datetime import date

from sqlmodel import Session, select

from eversafe_leads import db as dbmod
from eversafe_leads.enrich.companies import company_permit_stats, resolve_permit_contractors
from eversafe_leads.models import Company, Party, PartyRole, PartyStub, PermitDetail
from eversafe_leads.targets import target_list, to_csv

TODAY = date(2026, 7, 22)


def _seed(session: Session):
    juris = dbmod.get_or_create_jurisdiction(
        session,
        slug="orlando",
        display_name="City of Orlando",
        platform="socrata",
        base_url="https://x",
    )
    # Two fire permits by the same contractor (slightly different name spelling),
    # one commercial permit by another contractor.
    details = [
        PermitDetail(
            record_number="FIR-1",
            source_url="u1",
            record_type="Fire Permit",
            record_subtype="FireSupp",
            applied_date=TODAY,
            parties=[
                PartyStub(
                    role=PartyRole.contractor,
                    raw_name="WAYNE AUTOMATIC FIRE SPRINKLERS INC",
                    phone="(407)656-3030",
                )
            ],
        ),
        PermitDetail(
            record_number="FIR-2",
            source_url="u2",
            record_type="Fire Permit",
            record_subtype="FA",
            applied_date=TODAY,
            parties=[
                PartyStub(
                    role=PartyRole.contractor,
                    raw_name="Wayne Automatic Fire Sprinklers, LLC",
                    phone="407-656-3030",
                )
            ],
        ),
        PermitDetail(
            record_number="BLD-1",
            source_url="u3",
            record_type="Building Permit",
            record_subtype="Comm",
            applied_date=TODAY,
            parties=[PartyStub(role=PartyRole.contractor, raw_name="ACME BUILDERS INC")],
        ),
    ]
    for d in details:
        dbmod.persist_detail(session, juris.id, d)
    return juris


def test_resolve_and_stats_dedup_same_contractor():
    engine = dbmod.get_engine(":memory:")
    dbmod.init_db(engine)
    with Session(engine) as session:
        _seed(session)
        rstats = resolve_permit_contractors(session)
        assert rstats.parties_seen == 3
        # The two Wayne spellings resolve to ONE company (exact normalized name).
        companies = session.exec(select(Company)).all()
        names = {c.normalized_key for c in companies}
        assert "WAYNE AUTOMATIC FIRE SPRINKLERS" in names
        assert len(companies) == 2  # Wayne + Acme

        # Every contractor party is now linked.
        parties = session.exec(select(Party).where(Party.role == PartyRole.contractor)).all()
        assert all(p.company_id is not None for p in parties)

        stats = company_permit_stats(session, county_slug="orlando", today=TODAY)
        wayne = next(c for c in companies if "WAYNE" in c.normalized_key)
        assert stats[wayne.id]["permit_count"] == 2
        assert stats[wayne.id]["fire_permit_count"] == 2


def test_fire_active_ranking_and_csv():
    engine = dbmod.get_engine(":memory:")
    dbmod.init_db(engine)
    with Session(engine) as session:
        _seed(session)
        resolve_permit_contractors(session)
        rows = target_list(session, "fire-active", county_slug="orlando", today=TODAY)
        assert rows, "fire-active should return the Wayne contractor"
        assert "WAYNE" in rows[0].company.normalized_key
        assert rows[0].fire_permit_count == 2

        csv_text = to_csv(rows)
        assert "company,phone,licenses,permit_count,fire_permit_count,reason" in csv_text
        assert "WAYNE AUTOMATIC FIRE SPRINKLERS INC" in csv_text


def test_list_a_empty_without_flags():
    """List A is correctly empty until SFM/FBPE flags are populated."""
    engine = dbmod.get_engine(":memory:")
    dbmod.init_db(engine)
    with Session(engine) as session:
        _seed(session)
        resolve_permit_contractors(session)
        assert target_list(session, "A", county_slug="orlando", today=TODAY) == []

        # Once flagged as an FP contractor with no CA/PE, it appears.
        wayne = next(c for c in session.exec(select(Company)).all() if "WAYNE" in c.normalized_key)
        wayne.is_fp_contractor = True
        session.add(wayne)
        session.commit()
        rows = target_list(session, "A", county_slug="orlando", today=TODAY)
        assert len(rows) == 1 and "WAYNE" in rows[0].company.normalized_key
