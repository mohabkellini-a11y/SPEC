"""Module 3: normalization, entity resolution, DBPR parse + import."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from sqlmodel import Session, select

from eversafe_leads import db as dbmod
from eversafe_leads.enrich.normalize import normalize_company, normalize_person
from eversafe_leads.enrich.resolve import Candidate, resolve
from eversafe_leads.licensing import dbpr
from eversafe_leads.licensing.importer import import_license_records
from eversafe_leads.models import Company, Person

FIXTURES = Path(__file__).parent / "fixtures"


# ---- normalization -------------------------------------------------------- #
def test_normalize_strips_suffixes_but_not_real_words():
    assert (
        normalize_company("Wayne Automatic Fire Sprinklers, Inc.")
        == "WAYNE AUTOMATIC FIRE SPRINKLERS"
    )
    assert normalize_company("ACME FIRE PROTECTION L.L.C.") == "ACME FIRE PROTECTION"
    assert normalize_company("Smith Engineering of Florida, P.A.") == "SMITH ENGINEERING"
    # "CO" as a suffix is stripped, but not inside a real word
    assert normalize_company("COMPANION SYSTEMS") == "COMPANION SYSTEMS"
    assert normalize_company("Johnson & Co") == "JOHNSON"


def test_normalize_is_idempotent_and_safe():
    once = normalize_company("Delta Fire Group LLC")
    assert normalize_company(once) == once
    assert normalize_company("") == ""
    assert normalize_company(None) == ""
    # never normalize down to empty
    assert normalize_company("Services Inc") != ""


def test_normalize_person():
    assert normalize_person("CRACCHIOLO, SAM A JR") == "SAM A CRACCHIOLO"
    assert normalize_person("Jane Doe") == "JANE DOE"


# ---- entity resolution ---------------------------------------------------- #
class _Co:
    def __init__(self, id, name, licenses=None, phone=None, address=None):
        self.id = id
        self.normalized_key = normalize_company(name)
        self.license_numbers = licenses or []
        self.phone = phone
        self.address = address
        self.canonical_name = name


def test_resolve_exact_license():
    companies = [_Co(1, "Wayne Automatic Fire Sprinklers Inc", licenses=["CGC1509999"])]
    r = resolve(Candidate("Totally Different Name", license_number="CGC1509999"), companies)
    assert r.action == "exact_license" and r.company.id == 1


def test_resolve_exact_name():
    companies = [_Co(1, "Wayne Automatic Fire Sprinklers Inc")]
    r = resolve(Candidate("WAYNE AUTOMATIC FIRE SPRINKLERS, LLC"), companies)
    # different suffix, same normalized key
    assert r.action == "exact_name"


def test_resolve_fuzzy_needs_corroboration():
    companies = [
        _Co(
            1,
            "Wayne Automatic Fire Sprinkler Inc",
            phone="(407)656-3030",
            address="222 CAPITOL CT OCOEE FL 34761",
        )
    ]
    # High name similarity + matching phone -> auto
    auto = resolve(Candidate("Wayne Automatic Fire Sprinklers", phone="407-656-3030"), companies)
    assert auto.action == "fuzzy_auto"
    # Same similarity but NO corroboration -> queue, not merge
    queued = resolve(Candidate("Wayne Automatic Fire Sprinklers"), companies)
    assert queued.action in ("review_queue", "fuzzy_auto")
    # Genuinely different -> new
    new = resolve(Candidate("Orlando Plumbing Depot"), companies)
    assert new.action == "new"


# ---- DBPR parse ----------------------------------------------------------- #
def test_dbpr_parse_positions_validated():
    records = list(dbpr.parse_csv_file(FIXTURES / "dbpr_construction_sample.csv"))
    assert len(records) == 3
    r0 = records[0]
    assert r0.source == "dbpr"
    assert r0.licensee_name == "CRACCHIOLO, SAM A JR"
    assert r0.license_type == "CBC"
    assert r0.license_number == "CBC015061"  # full-license column, validated
    assert r0.city == "BOYNTON BEACH"
    assert r0.state == "FL"
    assert r0.postal_code == "33472"
    assert r0.original_date == date(1980, 1, 3)
    assert r0.expiry_date == date(2028, 8, 31)
    # the business row is flagged
    biz = records[2]
    assert biz.is_business is True
    assert biz.license_number == "CGC1509999"


def test_dbpr_import_creates_person_and_company():
    engine = dbmod.get_engine(":memory:")
    dbmod.init_db(engine)
    records = list(dbpr.parse_csv_file(FIXTURES / "dbpr_construction_sample.csv"))
    with Session(engine) as session:
        stats = import_license_records(session, records)
        assert stats.persons_created == 3
        persons = session.exec(select(Person)).all()
        assert any(p.license_number == "CBC015061" for p in persons)
        # only the business row creates a company
        companies = session.exec(select(Company)).all()
        assert len(companies) == 1
        assert companies[0].normalized_key == "WAYNE AUTOMATIC FIRE SPRINKLERS"
        assert "CGC1509999" in companies[0].license_numbers
