"""Module 4: seal extraction cascade, FBPE validation, and intelligence."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from sqlmodel import Session, select

from eversafe_leads import db as dbmod
from eversafe_leads.models import (
    Document,
    ExtractionMethod,
    PartyRole,
    PartyStub,
    PermitDetail,
    Person,
    Seal,
)
from eversafe_leads.seals import pipeline
from eversafe_leads.seals.extract import extract_seal
from eversafe_leads.seals.intelligence import engineer_summary, single_engineer_contractors

FIXTURES = Path(__file__).parent / "fixtures"
SEAL_PDF = FIXTURES / "seal_sample.pdf"

SEAL_TEXT = (
    "JOHN A. SMITH, P.E.\nFLORIDA LICENSE NO. 54321\n"
    "DISCIPLINE: FIRE PROTECTION\nCERTIFICATE OF AUTHORIZATION CA 9999"
)


# ---- pure extraction ------------------------------------------------------ #
def test_extract_seal_from_text():
    c = extract_seal(SEAL_TEXT)
    assert c.license_number == "PE54321"
    assert c.engineer_name == "JOHN A. SMITH"
    assert c.discipline == "Fire Protection"
    assert c.ca_number == "CA9999"
    assert "54321" in c.raw_text


def test_extract_seal_alt_notations():
    assert extract_seal("FL PE 12345").license_number == "PE12345"
    assert extract_seal("P.E. #6789").license_number == "PE6789"
    assert extract_seal("nothing here").is_empty


# ---- PDF text layer + cascade (needs pdfplumber) -------------------------- #
def test_pipeline_pdf_text():
    pytest.importorskip("pdfplumber")
    extractions = pipeline.extract_from_pdf(SEAL_PDF, want_ocr=False)
    assert extractions, "should extract a seal from the fixture PDF"
    ex = extractions[0]
    assert ex.method == ExtractionMethod.pdf_text
    assert ex.confidence == pipeline.CONF_TEXT
    assert ex.candidate.license_number == "PE54321"
    assert ex.candidate.engineer_name == "JOHN A. SMITH"


def test_persist_and_fbpe_validation():
    pytest.importorskip("pdfplumber")
    engine = dbmod.get_engine(":memory:")
    dbmod.init_db(engine)
    with Session(engine) as session:
        juris = dbmod.get_or_create_jurisdiction(
            session,
            slug="seminole",
            display_name="Seminole",
            platform="accela",
            base_url="https://x",
        )
        # A permit with a contractor (the client) + a document to seal.
        permit, _ = dbmod.persist_detail(
            session,
            juris.id,
            PermitDetail(
                record_number="SEM-1",
                source_url="u",
                record_type="Fire Permit",
                record_subtype="FireSupp",
                applied_date=date(2026, 3, 1),
                parties=[PartyStub(role=PartyRole.contractor, raw_name="BLAZE FIRE SYSTEMS LLC")],
            ),
        )
        doc = Document(permit_id=permit.id, title="FP-1", portal_url="https://x/fp1.pdf")
        session.add(doc)
        # FBPE person so the license validates.
        session.add(
            Person(
                name="John A Smith",
                license_number="PE54321",
                license_type="PE",
                discipline="Fire Protection",
            )
        )
        session.commit()
        session.refresh(doc)

        extractions = pipeline.extract_from_pdf(SEAL_PDF, want_ocr=False)
        seals = pipeline.persist_seals(session, doc.id, extractions)
        assert len(seals) == 1
        assert seals[0].license_number == "PE54321"
        assert "FBPE-validated" in seals[0].raw_text  # cross-checked against Person

        # Intelligence: engineer -> seal count, client, county.
        rows = engineer_summary(session, county="seminole", since=date(2025, 1, 1))
        assert len(rows) == 1
        r = rows[0]
        assert r.license_number == "PE54321"
        assert r.seal_count == 1
        assert "BLAZE FIRE SYSTEMS LLC" in r.clients
        assert r.counties == {"seminole"}

        # Blaze relies on exactly one engineer.
        assert ("BLAZE FIRE SYSTEMS LLC", "PE54321") in single_engineer_contractors(session)


def test_engineer_summary_empty_db():
    engine = dbmod.get_engine(":memory:")
    dbmod.init_db(engine)
    with Session(engine) as session:
        assert engineer_summary(session) == []
        assert session.exec(select(Seal)).all() == []
