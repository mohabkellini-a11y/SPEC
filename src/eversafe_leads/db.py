"""SQLite storage over SQLModel. One file at ``data/leads.db`` (SPEC §3)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import text
from sqlmodel import Session, SQLModel, create_engine, select

from . import models
from .models import (
    Document,
    Jurisdiction,
    Party,
    Permit,
    PermitDetail,
    Review,
)

DEFAULT_DB_PATH = Path("data/leads.db")


def get_engine(db_path: Path | str = DEFAULT_DB_PATH, echo: bool = False):
    db_path = Path(db_path)
    if db_path.name != ":memory:":
        db_path.parent.mkdir(parents=True, exist_ok=True)
    url = "sqlite://" if str(db_path) == ":memory:" else f"sqlite:///{db_path}"
    engine = create_engine(url, echo=echo)
    return engine


def init_db(engine) -> None:
    # Import side effect: ensure every table class is registered on the metadata.
    _ = models
    SQLModel.metadata.create_all(engine)
    with engine.connect() as conn:
        conn.execute(text("PRAGMA journal_mode=WAL"))


def get_or_create_jurisdiction(
    session: Session,
    *,
    slug: str,
    display_name: str,
    platform: str,
    base_url: str,
    timezone: str = "America/New_York",
    notes: str | None = None,
) -> Jurisdiction:
    existing = session.exec(select(Jurisdiction).where(Jurisdiction.slug == slug)).first()
    if existing:
        return existing
    juris = Jurisdiction(
        slug=slug,
        display_name=display_name,
        platform=platform,
        base_url=base_url,
        timezone=timezone,
        notes=notes,
    )
    session.add(juris)
    session.commit()
    session.refresh(juris)
    return juris


def upsert_permit(
    session: Session, jurisdiction_id: int, detail: PermitDetail
) -> tuple[Permit, bool]:
    """Insert a permit or refresh an existing one.

    Returns ``(permit, created)``. Existing rows keep ``first_seen_at`` and get
    ``last_seen_at`` bumped plus any newly-observed fields overwritten — this is
    what lets Module 2 diff review cycles across runs without losing history.
    """
    now = datetime.now(UTC)
    stmt = select(Permit).where(
        Permit.jurisdiction_id == jurisdiction_id,
        Permit.record_number == detail.record_number,
    )
    permit = session.exec(stmt).first()
    created = permit is None

    fields = detail.model_dump(exclude={"parties", "reviews", "documents"})
    if permit is None:
        permit = Permit(jurisdiction_id=jurisdiction_id, **fields)
        session.add(permit)
    else:
        for key, value in fields.items():
            if value is not None:
                setattr(permit, key, value)
        permit.last_seen_at = now

    session.commit()
    session.refresh(permit)
    return permit, created


def _upsert_parties(session: Session, permit_id: int, detail: PermitDetail) -> None:
    for stub in detail.parties:
        existing = session.exec(
            select(Party).where(
                Party.permit_id == permit_id,
                Party.role == stub.role,
                Party.raw_name == stub.raw_name,
            )
        ).first()
        if existing:
            existing.last_seen_at = datetime.now(UTC)
            for key in ("license_number", "phone", "email", "address"):
                value = getattr(stub, key)
                if value is not None:
                    setattr(existing, key, value)
        else:
            session.add(
                Party(
                    permit_id=permit_id,
                    role=stub.role,
                    raw_name=stub.raw_name,
                    license_number=stub.license_number,
                    phone=stub.phone,
                    email=stub.email,
                    address=stub.address,
                    source_url=detail.source_url,
                )
            )


def _upsert_reviews(session: Session, permit_id: int, detail: PermitDetail) -> list[Review]:
    """Insert new review rows; return the ones newly created (for Module 2)."""
    created: list[Review] = []
    for stub in detail.reviews:
        existing = session.exec(
            select(Review).where(
                Review.permit_id == permit_id,
                Review.cycle_number == stub.cycle_number,
                Review.department == stub.department,
            )
        ).first()
        if existing:
            existing.last_seen_at = datetime.now(UTC)
            if stub.status is not None:
                existing.status = stub.status
            if stub.comment_text is not None:
                existing.comment_text = stub.comment_text
        else:
            review = Review(
                permit_id=permit_id,
                cycle_number=stub.cycle_number,
                department=stub.department,
                reviewer_name=stub.reviewer_name,
                status=stub.status,
                status_date=stub.status_date,
                due_date=stub.due_date,
                comment_text=stub.comment_text,
                source_url=detail.source_url,
            )
            session.add(review)
            created.append(review)
    return created


def _upsert_documents(session: Session, permit_id: int, detail: PermitDetail) -> None:
    for ref in detail.documents:
        existing = session.exec(
            select(Document).where(
                Document.permit_id == permit_id,
                Document.portal_url == ref.portal_url,
            )
        ).first()
        if existing:
            existing.last_seen_at = datetime.now(UTC)
        else:
            session.add(Document(permit_id=permit_id, title=ref.title, portal_url=ref.portal_url))


def persist_detail(
    session: Session, jurisdiction_id: int, detail: PermitDetail
) -> tuple[Permit, bool]:
    """Persist a permit and its parties, reviews, and documents in one shot.

    Returns ``(permit, created)`` for the permit row. Review rows created on
    this call are what Module 2 will diff; nothing is ever deleted.
    """
    permit, created = upsert_permit(session, jurisdiction_id, detail)
    _upsert_parties(session, permit.id, detail)
    _upsert_reviews(session, permit.id, detail)
    _upsert_documents(session, permit.id, detail)
    session.commit()
    return permit, created
