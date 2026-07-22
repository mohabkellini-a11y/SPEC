"""Data models.

Two layers live here:

* **SQLModel tables** — the persisted schema (SPEC §5). Every row that comes off
  a portal carries ``source_url``, ``first_seen_at``, ``last_seen_at`` and
  ``raw_payload`` so nothing an adapter saw is ever lost.
* **Transient DTOs** — plain pydantic models an adapter yields
  (``PermitStub`` → ``PermitDetail`` → ``DocumentRef``) before anything is
  written to the DB. Parsing failures on these are validation errors, never
  silent ``None`` (SPEC §2).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict
from sqlalchemy import Column, UniqueConstraint
from sqlalchemy import Enum as SAEnum
from sqlalchemy.types import JSON
from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    return datetime.now(UTC)


# --------------------------------------------------------------------------- #
# Enums
# --------------------------------------------------------------------------- #
class PartyRole(StrEnum):
    applicant = "applicant"
    owner = "owner"
    contractor = "contractor"
    engineer = "engineer"
    architect = "architect"
    agent = "agent"


class ExtractionMethod(StrEnum):
    digital_signature = "digital_signature"
    pdf_text = "pdf_text"
    ocr = "ocr"


# --------------------------------------------------------------------------- #
# Persisted tables (SPEC §5)
# --------------------------------------------------------------------------- #
class Jurisdiction(SQLModel, table=True):
    __tablename__ = "jurisdiction"

    id: int | None = Field(default=None, primary_key=True)
    slug: str = Field(index=True, unique=True)
    display_name: str
    platform: str
    base_url: str
    timezone: str = "America/New_York"
    notes: str | None = None


class Permit(SQLModel, table=True):
    __tablename__ = "permit"
    __table_args__ = (
        UniqueConstraint("jurisdiction_id", "record_number", name="uq_permit_juris_record"),
    )

    id: int | None = Field(default=None, primary_key=True)
    jurisdiction_id: int = Field(foreign_key="jurisdiction.id", index=True)
    record_number: str = Field(index=True)
    record_type: str | None = None
    record_subtype: str | None = None
    description: str | None = None
    status: str | None = None
    applied_date: date | None = None
    issued_date: date | None = None
    finaled_date: date | None = None
    valuation: float | None = None
    square_footage: float | None = None
    occupancy_type: str | None = None

    address_line1: str | None = None
    city: str | None = None
    state: str | None = None
    postal_code: str | None = None
    parcel_id: str | None = None
    lat: float | None = None
    lon: float | None = None

    portal_url: str | None = None
    review_cycles: int | None = None

    source_url: str
    first_seen_at: datetime = Field(default_factory=utcnow)
    last_seen_at: datetime = Field(default_factory=utcnow)
    raw_payload: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))


class Party(SQLModel, table=True):
    __tablename__ = "party"

    id: int | None = Field(default=None, primary_key=True)
    permit_id: int = Field(foreign_key="permit.id", index=True)
    role: PartyRole = Field(sa_column=Column(SAEnum(PartyRole)))
    raw_name: str
    company_id: int | None = Field(default=None, foreign_key="company.id")
    license_number: str | None = None
    phone: str | None = None
    email: str | None = None
    address: str | None = None

    source_url: str | None = None
    first_seen_at: datetime = Field(default_factory=utcnow)
    last_seen_at: datetime = Field(default_factory=utcnow)
    raw_payload: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))


class Review(SQLModel, table=True):
    __tablename__ = "review"
    __table_args__ = (
        UniqueConstraint(
            "permit_id", "cycle_number", "department", name="uq_review_permit_cycle_dept"
        ),
    )

    id: int | None = Field(default=None, primary_key=True)
    permit_id: int = Field(foreign_key="permit.id", index=True)
    cycle_number: int = 1
    department: str | None = None
    reviewer_name: str | None = None
    status: str | None = None
    status_date: date | None = None
    due_date: date | None = None
    comment_text: str | None = None

    source_url: str | None = None
    first_seen_at: datetime = Field(default_factory=utcnow)
    last_seen_at: datetime = Field(default_factory=utcnow)
    raw_payload: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))


class Document(SQLModel, table=True):
    __tablename__ = "document"

    id: int | None = Field(default=None, primary_key=True)
    permit_id: int = Field(foreign_key="permit.id", index=True)
    title: str | None = None
    portal_url: str
    local_path: str | None = None
    sha256: str | None = Field(default=None, index=True)
    page_count: int | None = None
    downloaded_at: datetime | None = None

    first_seen_at: datetime = Field(default_factory=utcnow)
    last_seen_at: datetime = Field(default_factory=utcnow)
    raw_payload: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))


class Seal(SQLModel, table=True):
    __tablename__ = "seal"

    id: int | None = Field(default=None, primary_key=True)
    document_id: int = Field(foreign_key="document.id", index=True)
    page_number: int | None = None
    engineer_name: str | None = None
    license_number: str | None = None
    discipline: str | None = None
    firm_name: str | None = None
    extraction_method: ExtractionMethod = Field(sa_column=Column(SAEnum(ExtractionMethod)))
    confidence: float = 0.0
    bbox: str | None = None
    raw_text: str | None = None

    first_seen_at: datetime = Field(default_factory=utcnow)
    last_seen_at: datetime = Field(default_factory=utcnow)


class Company(SQLModel, table=True):
    __tablename__ = "company"

    id: int | None = Field(default=None, primary_key=True)
    canonical_name: str
    normalized_key: str = Field(index=True)
    aliases: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    license_numbers: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    phone: str | None = None
    website: str | None = None
    address: str | None = None
    has_engineering_ca: bool = False
    has_fp_pe_on_record: bool = False
    is_fp_contractor: bool = False

    first_seen_at: datetime = Field(default_factory=utcnow)
    last_seen_at: datetime = Field(default_factory=utcnow)


class Person(SQLModel, table=True):
    __tablename__ = "person"

    id: int | None = Field(default=None, primary_key=True)
    name: str
    license_number: str | None = Field(default=None, index=True)
    license_type: str | None = None
    discipline: str | None = None
    status: str | None = None
    company_id: int | None = Field(default=None, foreign_key="company.id")


class Lead(SQLModel, table=True):
    __tablename__ = "lead"

    id: int | None = Field(default=None, primary_key=True)
    company_id: int | None = Field(default=None, foreign_key="company.id")
    permit_id: int | None = Field(default=None, foreign_key="permit.id")
    score: float = 0.0
    score_breakdown: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    stage: str = "new"
    next_action: str | None = None
    notes: str | None = None
    snoozed_until: date | None = None

    first_seen_at: datetime = Field(default_factory=utcnow)
    last_seen_at: datetime = Field(default_factory=utcnow)


class ReviewQueue(SQLModel, table=True):
    """Ambiguous entity-resolution matches (fuzzy 85–92) that must NOT auto-merge
    (SPEC §6). A human resolves these; nothing here is applied automatically."""

    __tablename__ = "review_queue"

    id: int | None = Field(default=None, primary_key=True)
    candidate_name: str
    candidate_normalized_key: str
    candidate_license: str | None = None
    candidate_phone: str | None = None
    candidate_address: str | None = None
    matched_company_id: int | None = Field(default=None, foreign_key="company.id")
    matched_company_name: str | None = None
    score: float = 0.0
    resolved: bool = False
    first_seen_at: datetime = Field(default_factory=utcnow)


class Signal(SQLModel, table=True):
    """A fired HOT signal (Module 2). ``dedup_key`` enforces "one alert per
    permit per review cycle, ever" (SPEC §6) via a unique index."""

    __tablename__ = "signal"

    id: int | None = Field(default=None, primary_key=True)
    permit_id: int = Field(foreign_key="permit.id", index=True)
    kind: str  # fire_review_rejection | second_fire_cycle | stuck_in_fire_review
    cycle_number: int = 1
    department: str | None = None
    status: str | None = None
    comment_text: str | None = None
    dedup_key: str = Field(unique=True, index=True)
    fired_at: datetime = Field(default_factory=utcnow)


# --------------------------------------------------------------------------- #
# Transient adapter DTOs (never persisted directly)
# --------------------------------------------------------------------------- #
class PartyStub(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: PartyRole
    raw_name: str
    license_number: str | None = None
    phone: str | None = None
    email: str | None = None
    address: str | None = None


class ReviewStub(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cycle_number: int = 1
    department: str | None = None
    reviewer_name: str | None = None
    status: str | None = None
    status_date: date | None = None
    due_date: date | None = None
    comment_text: str | None = None


class DocumentRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = None
    portal_url: str


class PermitStub(BaseModel):
    """The minimum an adapter's ``search`` yields before a detail fetch."""

    model_config = ConfigDict(extra="forbid")

    record_number: str
    record_type: str | None = None
    status: str | None = None
    applied_date: date | None = None
    address_line1: str | None = None
    portal_url: str | None = None
    source_url: str
    raw_payload: dict[str, Any] = {}


class PermitDetail(BaseModel):
    """A fully-populated permit an adapter yields, ready to persist."""

    model_config = ConfigDict(extra="forbid")

    record_number: str
    record_type: str | None = None
    record_subtype: str | None = None
    description: str | None = None
    status: str | None = None
    applied_date: date | None = None
    issued_date: date | None = None
    finaled_date: date | None = None
    valuation: float | None = None
    square_footage: float | None = None
    occupancy_type: str | None = None

    address_line1: str | None = None
    city: str | None = None
    state: str | None = None
    postal_code: str | None = None
    parcel_id: str | None = None
    lat: float | None = None
    lon: float | None = None

    portal_url: str | None = None
    review_cycles: int | None = None

    source_url: str
    raw_payload: dict[str, Any] = {}

    parties: list[PartyStub] = []
    reviews: list[ReviewStub] = []
    documents: list[DocumentRef] = []
