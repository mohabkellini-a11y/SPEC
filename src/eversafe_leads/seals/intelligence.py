"""Seal intelligence (Module 4 output).

Turns raw ``seal`` rows into the relationships EverSafe cares about: which
engineers seal the most work, who their clients are, which contractors lean on a
single engineer, and how far the sealing engineer sits from the job.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from sqlmodel import Session, select

from ..models import Document, Jurisdiction, Party, PartyRole, Permit, Seal


@dataclass
class EngineerRow:
    engineer_name: str | None
    license_number: str | None
    seal_count: int = 0
    disciplines: set[str] = field(default_factory=set)
    clients: set[str] = field(default_factory=set)
    counties: set[str] = field(default_factory=set)


def _seal_context(session: Session, county: str | None, since: date | None):
    """Yield (seal, permit, jurisdiction_slug, client_names) for each seal."""
    juris_by_id = {j.id: j.slug for j in session.exec(select(Jurisdiction)).all()}
    for seal in session.exec(select(Seal)).all():
        doc = session.get(Document, seal.document_id)
        permit = session.get(Permit, doc.permit_id) if doc else None
        slug = juris_by_id.get(permit.jurisdiction_id) if permit else None
        if county and slug != county:
            continue
        if since and permit and permit.applied_date and permit.applied_date < since:
            continue
        clients = []
        if permit:
            parties = session.exec(
                select(Party).where(
                    Party.permit_id == permit.id, Party.role == PartyRole.contractor
                )
            ).all()
            clients = [p.raw_name for p in parties]
        yield seal, permit, slug, clients


def engineer_summary(
    session: Session, county: str | None = None, since: date | None = None
) -> list[EngineerRow]:
    rows: dict[str, EngineerRow] = {}
    for seal, _permit, slug, clients in _seal_context(session, county, since):
        key = seal.license_number or (seal.engineer_name or "unknown")
        row = rows.setdefault(
            key, EngineerRow(engineer_name=seal.engineer_name, license_number=seal.license_number)
        )
        row.seal_count += 1
        if seal.discipline:
            row.disciplines.add(seal.discipline)
        row.clients.update(c for c in clients if c)
        if slug:
            row.counties.add(slug)
    return sorted(rows.values(), key=lambda r: r.seal_count, reverse=True)


def single_engineer_contractors(session: Session) -> list[tuple[str, str]]:
    """Contractors whose sealed work all traces to exactly one engineer —
    the ones most exposed if that engineer becomes unavailable."""
    contractor_engineers: dict[str, set[str]] = {}
    for seal, _permit, _slug, clients in _seal_context(session, None, None):
        eng = seal.license_number or seal.engineer_name
        if not eng:
            continue
        for c in clients:
            contractor_engineers.setdefault(c, set()).add(eng)
    return [(c, next(iter(e))) for c, e in contractor_engineers.items() if len(e) == 1]
