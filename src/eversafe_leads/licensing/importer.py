"""Land LicenseRecords into the DB (Person registry + Company resolution).

Individual licensees become ``person`` rows keyed by license number. Business
licenses (rows carrying a DBA / business name) are resolved against existing
``company`` rows via :mod:`eversafe_leads.enrich.resolve`; ambiguous fuzzy
matches (85–92) are parked in ``review_queue`` instead of auto-merging.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlmodel import Session, select

from ..enrich.normalize import normalize_company
from ..enrich.resolve import Candidate, resolve
from ..models import Company, Person, ReviewQueue
from .base import LicenseRecord


@dataclass
class ImportStats:
    persons_created: int = 0
    persons_updated: int = 0
    companies_created: int = 0
    companies_updated: int = 0
    queued_for_review: int = 0
    notes: list[str] = field(default_factory=list)


def _upsert_person(session: Session, rec: LicenseRecord) -> bool:
    """Return True if a new person was created."""
    existing = None
    if rec.license_number:
        existing = session.exec(
            select(Person).where(Person.license_number == rec.license_number)
        ).first()
    if existing:
        existing.status = rec.status_code or existing.status
        existing.license_type = rec.license_type or existing.license_type
        return False
    session.add(
        Person(
            name=rec.licensee_name,
            license_number=rec.license_number,
            license_type=rec.license_type,
            discipline=None,
            status=rec.status_code,
        )
    )
    return True


def _upsert_company(session: Session, rec: LicenseRecord, stats: ImportStats) -> None:
    name = rec.business_name or rec.licensee_name
    companies = session.exec(select(Company)).all()
    candidate = Candidate(
        name=name,
        license_number=rec.license_number,
        phone=rec.phone,
        address=rec.address,
    )
    result = resolve(candidate, companies)

    if result.action in ("exact_license", "exact_name", "fuzzy_auto") and result.company:
        company = result.company
        if rec.license_number and rec.license_number not in (company.license_numbers or []):
            company.license_numbers = [*(company.license_numbers or []), rec.license_number]
        company.last_seen_at = datetime.now(UTC)
        stats.companies_updated += 1
    elif result.action == "review_queue" and result.company:
        session.add(
            ReviewQueue(
                candidate_name=name,
                candidate_normalized_key=candidate.normalized_key,
                candidate_license=rec.license_number,
                candidate_phone=rec.phone,
                candidate_address=rec.address,
                matched_company_id=result.company.id,
                matched_company_name=result.company.canonical_name,
                score=result.score,
            )
        )
        stats.queued_for_review += 1
    else:  # new
        session.add(
            Company(
                canonical_name=name,
                normalized_key=normalize_company(name),
                license_numbers=[rec.license_number] if rec.license_number else [],
                phone=rec.phone,
                address=rec.address,
            )
        )
        stats.companies_created += 1


def import_license_records(
    session: Session,
    records,
    limit: int | None = None,
    commit_every: int = 1000,
) -> ImportStats:
    stats = ImportStats()
    n = 0
    for i, rec in enumerate(records):
        if limit is not None and i >= limit:
            break
        if _upsert_person(session, rec):
            stats.persons_created += 1
        else:
            stats.persons_updated += 1
        if rec.is_business:
            _upsert_company(session, rec, stats)
        n += 1
        if n % commit_every == 0:
            session.commit()
    session.commit()
    return stats
