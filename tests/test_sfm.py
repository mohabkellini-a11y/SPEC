"""SFM roster import completes Target List A."""

from __future__ import annotations

from datetime import date

from sqlmodel import Session, select

from eversafe_leads import db as dbmod
from eversafe_leads.enrich.companies import resolve_permit_contractors
from eversafe_leads.licensing import fbpe, sfm
from eversafe_leads.models import Company, PartyRole, PartyStub, PermitDetail
from eversafe_leads.targets import target_list

TODAY = date(2026, 7, 22)

SFM_CSV = """business_name,license_number,license_class,status,qualifier_name,phone,address,city,state
Blaze Fire Systems LLC,FC0012345,Contractor I,Current,"Reed, Sam",(407)555-0300,10 Elm St,Orlando,FL
Meridian MEP Engineers LLC,FC0067890,Contractor IV,Current,"Alvarez, Maria",(407)555-0100,100 N Orange Ave,Orlando,FL
"""

CA_CSV = """business_name,ca_number,qualifier_name,qualifier_license,qualifier_discipline,status
Meridian MEP Engineers LLC,CA00028841,"Alvarez, Maria",PE58122,Mechanical,Current
"""


def _seed(session):
    juris = dbmod.get_or_create_jurisdiction(
        session,
        slug="orlando",
        display_name="City of Orlando",
        platform="socrata",
        base_url="https://x",
    )
    for firm, rec in [("BLAZE FIRE SYSTEMS LLC", "FIR-1"), ("MERIDIAN MEP ENGINEERS LLC", "ENG-1")]:
        dbmod.persist_detail(
            session,
            juris.id,
            PermitDetail(
                record_number=rec,
                source_url="u",
                record_type="Fire Permit",
                record_subtype="FireSupp",
                applied_date=TODAY,
                parties=[PartyStub(role=PartyRole.contractor, raw_name=firm)],
            ),
        )
    resolve_permit_contractors(session)


def test_sfm_parse():
    recs = list(sfm.parse_sfm_roster(SFM_CSV))
    assert len(recs) == 2
    assert recs[0].source == "sfm"
    assert recs[0].business_name == "Blaze Fire Systems LLC"
    assert recs[0].license_number == "FC0012345"
    assert recs[0].is_business


def test_list_a_end_to_end():
    engine = dbmod.get_engine(":memory:")
    dbmod.init_db(engine)
    with Session(engine) as session:
        _seed(session)
        stats = sfm.apply_sfm_records(session, sfm.parse_sfm_roster(SFM_CSV))
        assert stats.firms_flagged == 2

        # Both are now FP contractors; before any CA, both qualify for List A.
        names = {
            r.company.normalized_key
            for r in target_list(session, "A", county_slug="orlando", today=TODAY)
        }
        assert "BLAZE FIRE SYSTEMS" in names
        assert "MERIDIAN MEP ENGINEERS" in names

        # Meridian ALSO holds a CA -> it drops off List A (has_engineering_ca).
        fbpe.apply_ca_records(session, fbpe.parse_ca_roster(CA_CSV))
        rows = target_list(session, "A", county_slug="orlando", today=TODAY)
        names2 = {r.company.normalized_key for r in rows}
        assert "BLAZE FIRE SYSTEMS" in names2
        assert "MERIDIAN MEP ENGINEERS" not in names2  # excluded: now has a CA

        blaze = next(c for c in session.exec(select(Company)).all() if "BLAZE" in c.normalized_key)
        assert blaze.is_fp_contractor and "FC0012345" in blaze.license_numbers
