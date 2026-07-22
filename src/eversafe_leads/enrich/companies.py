"""Resolve permit parties into companies and compute permit volume (Module 3).

This is the bridge between Module 1 (permits + their contractor/engineer/
architect parties) and the target lists: every contractor/engineer/architect
party is resolved to a ``company`` row (deduped via
:mod:`eversafe_leads.enrich.resolve`), and ``party.company_id`` is linked so
permit volume can be counted per company.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlmodel import Session, select

from ..models import Company, Party, PartyRole, Permit
from .normalize import normalize_company
from .resolve import Candidate, resolve

# Party roles that represent firms we might target.
FIRM_ROLES = {PartyRole.contractor, PartyRole.engineer, PartyRole.architect}


@dataclass
class ResolveStats:
    parties_seen: int = 0
    companies_created: int = 0
    linked: int = 0
    notes: list[str] = field(default_factory=list)


def resolve_permit_contractors(session: Session) -> ResolveStats:
    """Link every firm-role party to a company row, creating companies as needed.

    Idempotent: re-running only fills in missing links and never duplicates a
    company whose normalized name already exists.
    """
    stats = ResolveStats()
    # Load companies once and keep an in-memory index; exact-name hits are then
    # O(1) and the expensive fuzzy scan only runs for genuinely new names. New
    # companies are flushed (not committed) to get their id, then appended to the
    # in-memory list so later parties resolve against them without a re-query.
    companies: list[Company] = list(session.exec(select(Company)).all())
    by_key: dict[str, Company] = {c.normalized_key: c for c in companies if c.normalized_key}

    parties = session.exec(select(Party).where(Party.role.in_(FIRM_ROLES))).all()  # type: ignore[attr-defined]
    for party in parties:
        stats.parties_seen += 1
        key = normalize_company(party.raw_name)
        company = by_key.get(key) if key else None

        if company is None:
            candidate = Candidate(name=party.raw_name, phone=party.phone, address=party.address)
            result = resolve(candidate, companies)
            if result.company is not None and result.action in (
                "exact_license",
                "exact_name",
                "fuzzy_auto",
            ):
                company = result.company
            else:
                company = Company(
                    canonical_name=party.raw_name,
                    normalized_key=key,
                    phone=party.phone,
                    address=party.address,
                )
                session.add(company)
                session.flush()  # assign id without a full commit
                companies.append(company)
                if key:
                    by_key[key] = company
                stats.companies_created += 1

        if party.company_id != company.id:
            party.company_id = company.id
            stats.linked += 1
    session.commit()
    return stats


def company_permit_stats(
    session: Session,
    county_slug: str | None = None,
    months: int = 12,
    today: date | None = None,
) -> dict[int, dict[str, int]]:
    """Per-company permit counts (total + fire) within the trailing window.

    Fire permits are those whose ``record_type`` mentions fire or whose
    ``record_subtype`` is a fire worktype (``FireSupp``/``FA``).
    """
    today = today or date.today()
    cutoff = today - timedelta(days=months * 30)

    juris_id = None
    if county_slug:
        from ..models import Jurisdiction

        juris = session.exec(select(Jurisdiction).where(Jurisdiction.slug == county_slug)).first()
        if juris is None:
            return {}
        juris_id = juris.id

    rows = session.exec(
        select(Party.company_id, Permit)
        .join(Permit, Party.permit_id == Permit.id)
        .where(
            Party.role == PartyRole.contractor,
            Party.company_id.is_not(None),  # type: ignore[union-attr]
        )
    ).all()

    stats: dict[int, dict[str, int]] = {}
    for company_id, permit in rows:
        if juris_id is not None and permit.jurisdiction_id != juris_id:
            continue
        if permit.applied_date and permit.applied_date < cutoff:
            continue
        entry = stats.setdefault(company_id, {"permit_count": 0, "fire_permit_count": 0})
        entry["permit_count"] += 1
        is_fire = (permit.record_type and "fire" in permit.record_type.lower()) or (
            permit.record_subtype in ("FireSupp", "FA")
        )
        if is_fire:
            entry["fire_permit_count"] += 1
    return stats
