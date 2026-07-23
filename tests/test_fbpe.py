"""FBPE roster import: CA flags light up Target List B; FP-discipline excludes."""

from __future__ import annotations

from datetime import date

from sqlmodel import Session, select

from eversafe_leads import db as dbmod
from eversafe_leads.enrich.companies import resolve_permit_contractors
from eversafe_leads.licensing import fbpe
from eversafe_leads.models import Company, PartyRole, PartyStub, PermitDetail, Person
from eversafe_leads.targets import target_list

TODAY = date(2026, 7, 22)

CA_CSV = """business_name,ca_number,qualifier_name,qualifier_license,qualifier_discipline,status,phone,address
Meridian MEP Engineers LLC,CA00028841,"Alvarez, Maria",PE58122,Mechanical,Current,(407)555-0100,100 N Orange Ave Orlando FL 32801
Ignis Fire Protection Engineering Inc,CA00030777,"Okafor, David",PE61903,Fire Protection,Current,(407)555-0200,200 S Eola Dr Orlando FL 32801
"""

PE_CSV = """license_number,name,discipline,status,city,state
PE58122,"Alvarez, Maria",Mechanical,Current,Orlando,FL
PE61903,"Okafor, David",Fire Protection,Current,Orlando,FL
"""


def test_fire_discipline_matcher():
    assert fbpe.is_fire_discipline("Fire Protection")
    assert fbpe.is_fire_discipline("FIRE PROTECTION ENGINEERING")
    assert fbpe.is_fire_discipline("Life Safety")
    assert not fbpe.is_fire_discipline("Mechanical")
    assert not fbpe.is_fire_discipline(None)


def test_pe_roster_parse():
    recs = list(fbpe.parse_pe_roster(PE_CSV))
    assert len(recs) == 2
    fp = next(r for r in recs if r.license_number == "PE61903")
    assert fp.discipline == "Fire Protection"
    assert fp.license_type == "PE"


def test_ca_flags_and_list_b_end_to_end():
    engine = dbmod.get_engine(":memory:")
    dbmod.init_db(engine)
    with Session(engine) as session:
        juris = dbmod.get_or_create_jurisdiction(
            session,
            slug="orlando",
            display_name="City of Orlando",
            platform="socrata",
            base_url="https://x",
        )
        # Both firms pull a recent commercial permit (so they have permit volume).
        for firm, rec in [
            ("MERIDIAN MEP ENGINEERS LLC", "ENG-1"),
            ("IGNIS FIRE PROTECTION ENGINEERING INC", "ENG-2"),
        ]:
            dbmod.persist_detail(
                session,
                juris.id,
                PermitDetail(
                    record_number=rec,
                    source_url="u",
                    record_type="Engineering",
                    record_subtype="Comm",
                    applied_date=TODAY,
                    parties=[PartyStub(role=PartyRole.contractor, raw_name=firm)],
                ),
            )
        resolve_permit_contractors(session)

        # Import the CA + PE rosters.
        fbpe.apply_pe_records(session, fbpe.parse_pe_roster(PE_CSV))
        stats = fbpe.apply_ca_records(session, fbpe.parse_ca_roster(CA_CSV))
        assert stats.ca_firms_flagged == 2
        assert stats.fp_pe_firms == 1  # only Ignis has an FP PE

        companies = session.exec(select(Company)).all()
        meridian = next(c for c in companies if "MERIDIAN" in c.normalized_key)
        ignis = next(c for c in companies if "IGNIS" in c.normalized_key)
        assert meridian.has_engineering_ca and not meridian.has_fp_pe_on_record
        assert ignis.has_engineering_ca and ignis.has_fp_pe_on_record

        # List B = has CA, no FP PE, has permit volume -> Meridian only.
        rows = target_list(session, "B", county_slug="orlando", today=TODAY)
        names = {r.company.normalized_key for r in rows}
        assert "MERIDIAN MEP ENGINEERS" in names
        assert "IGNIS FIRE PROTECTION ENGINEERING" not in names  # excluded: has FP PE

        # The FP PE landed in the person registry with its discipline.
        okafor = session.exec(select(Person).where(Person.license_number == "PE61903")).first()
        assert okafor and fbpe.is_fire_discipline(okafor.discipline)
