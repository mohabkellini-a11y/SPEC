"""FBPE collector — Florida Board of Professional Engineers (Module 3).

Access reality (verified 2026-07-22): FBPE's licensee search delegates to an
opaque ASP search and the site sets ``Crawl-delay: 30`` (one request / 30 s);
the "engineering directory" download links resolve to HTML landing pages, not
data files. Per SPEC §6 the preferred path here is the **directory / Ch. 119
records request** rather than per-name scraping — so this collector imports a
roster (CSV) that the practice obtains once, and applies it to the graph.

Two rosters:

* **PE roster** — individual licensees with discipline. Feeds the ``person``
  registry and marks who the Fire-Protection PEs are.
* **CA roster** — Certificates of Authorization held by firms, each listing the
  qualifying engineer and their discipline. This is the link that lets us set,
  per firm, ``has_engineering_ca`` and ``has_fp_pe_on_record`` — the two flags
  Target List B turns on.

If/when a live per-name lookup is wired in, it MUST honor the 30 s crawl-delay;
it is intentionally not faked here (SPEC §2: never invent a selector).
"""

from __future__ import annotations

import csv
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field

from sqlmodel import Session, select

from ..enrich.normalize import normalize_company
from ..enrich.resolve import Candidate, resolve
from ..models import Company, Person
from .base import LicenseRecord

# A Fire-Protection discipline is the signal for has_fp_pe_on_record.
FIRE_DISCIPLINE_RE = re.compile(r"fire\s*protection|fire|life\s*safety", re.IGNORECASE)


def is_fire_discipline(discipline: str | None) -> bool:
    return bool(discipline and FIRE_DISCIPLINE_RE.search(discipline))


def _get(row: dict[str, str], *keys: str) -> str | None:
    for k in keys:
        for actual in row:
            if actual.strip().lower() == k:
                v = (row[actual] or "").strip()
                return v or None
    return None


# --------------------------------------------------------------------------- #
# PE roster
# --------------------------------------------------------------------------- #
def parse_pe_roster(csv_text: str) -> Iterator[LicenseRecord]:
    """Header-based CSV: license_number, name, discipline, status, city, state."""
    for row in csv.DictReader(csv_text.splitlines()):
        name = _get(row, "name", "licensee_name", "engineer_name")
        if not name:
            continue
        yield LicenseRecord(
            source="fbpe",
            licensee_name=name,
            license_type="PE",
            license_number=_get(row, "license_number", "license", "pe_number", "pe"),
            discipline=_get(row, "discipline"),
            status_code=_get(row, "status"),
            city=_get(row, "city"),
            state=_get(row, "state"),
            is_business=False,
        )


# --------------------------------------------------------------------------- #
# CA roster (firm -> qualifying PE -> discipline)
# --------------------------------------------------------------------------- #
@dataclass
class CARecord:
    business_name: str
    ca_number: str | None = None
    qualifier_name: str | None = None
    qualifier_license: str | None = None
    qualifier_discipline: str | None = None
    status: str | None = None
    phone: str | None = None
    address: str | None = None


def parse_ca_roster(csv_text: str) -> Iterator[CARecord]:
    """Header-based CSV: business_name, ca_number, qualifier_name,
    qualifier_license, qualifier_discipline, status, phone, address."""
    for row in csv.DictReader(csv_text.splitlines()):
        name = _get(row, "business_name", "firm", "company", "name")
        if not name:
            continue
        yield CARecord(
            business_name=name,
            ca_number=_get(row, "ca_number", "ca", "certificate", "certificate_number"),
            qualifier_name=_get(row, "qualifier_name", "qualifier", "engineer", "engineer_name"),
            qualifier_license=_get(row, "qualifier_license", "pe_license", "pe_number"),
            qualifier_discipline=_get(row, "qualifier_discipline", "discipline"),
            status=_get(row, "status"),
            phone=_get(row, "phone"),
            address=_get(row, "address"),
        )


# --------------------------------------------------------------------------- #
# Apply to the graph
# --------------------------------------------------------------------------- #
@dataclass
class FBPEStats:
    pe_persons: int = 0
    ca_firms_flagged: int = 0
    fp_pe_firms: int = 0
    notes: list[str] = field(default_factory=list)


def apply_pe_records(session: Session, records: Iterable[LicenseRecord]) -> int:
    """Upsert PE licensees into the person registry (with discipline)."""
    n = 0
    for rec in records:
        existing = None
        if rec.license_number:
            existing = session.exec(
                select(Person).where(Person.license_number == rec.license_number)
            ).first()
        if existing:
            existing.discipline = rec.discipline or existing.discipline
            existing.status = rec.status_code or existing.status
        else:
            session.add(
                Person(
                    name=rec.licensee_name,
                    license_number=rec.license_number,
                    license_type="PE",
                    discipline=rec.discipline,
                    status=rec.status_code,
                )
            )
            n += 1
    session.commit()
    return n


def apply_ca_records(session: Session, records: Iterable[CARecord]) -> FBPEStats:
    """Set has_engineering_ca / has_fp_pe_on_record on firms from CA records.

    A CA marks the firm as holding a Certificate of Authorization. If its
    qualifying engineer's discipline is Fire Protection, the firm also has an
    FP PE on record — which removes it from Target List B (it does not need to
    outsource FP stamping).
    """
    stats = FBPEStats()
    companies = list(session.exec(select(Company)).all())

    for ca in records:
        candidate = Candidate(name=ca.business_name, phone=ca.phone, address=ca.address)
        result = resolve(candidate, companies)
        if result.company is not None and result.action in (
            "exact_license",
            "exact_name",
            "fuzzy_auto",
        ):
            company = result.company
        else:
            company = Company(
                canonical_name=ca.business_name,
                normalized_key=normalize_company(ca.business_name),
                phone=ca.phone,
                address=ca.address,
            )
            session.add(company)
            session.flush()
            companies.append(company)

        company.has_engineering_ca = True
        if ca.ca_number and ca.ca_number not in (company.license_numbers or []):
            company.license_numbers = [*(company.license_numbers or []), ca.ca_number]
        stats.ca_firms_flagged += 1

        if is_fire_discipline(ca.qualifier_discipline):
            company.has_fp_pe_on_record = True
            stats.fp_pe_firms += 1

        # Register the qualifying engineer as a person, with discipline.
        if ca.qualifier_name:
            existing = None
            if ca.qualifier_license:
                existing = session.exec(
                    select(Person).where(Person.license_number == ca.qualifier_license)
                ).first()
            if existing is None:
                session.add(
                    Person(
                        name=ca.qualifier_name,
                        license_number=ca.qualifier_license,
                        license_type="PE",
                        discipline=ca.qualifier_discipline,
                        company_id=company.id,
                    )
                )
    session.commit()
    return stats
