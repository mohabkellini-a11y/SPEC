"""SFM collector — Florida Division of State Fire Marshal (Module 3).

The F.S. Chapter 633 fire-protection contractor licenses (Contractor I–V) and
their business entities are administered through the SFM's **CitizenServe**
portal (`citizenserve.com/120/`). Verified 2026-07-22: that portal is a heavy
JS app whose license search renders results via XHR — the static HTML exposes
only permit/complaint AJAX actions, no scrapeable license-search endpoint or
result schema. Per SPEC §6 (prefer records request, never invent a selector),
this collector imports a roster CSV obtained via the portal export / a Ch. 119
records request rather than scraping.

Applying an SFM roster sets ``is_fp_contractor = True`` on the matching firm —
the flag Target List A needs. (The DFS `licenseesearch.fldfs.com` "Industrial
Fire & Burglary" category is alarm/insurance agents, NOT these Ch. 633 system
contractors, so it is deliberately not used here.)
"""

from __future__ import annotations

import csv
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field

from sqlmodel import Session, select

from ..enrich.normalize import normalize_company
from ..enrich.resolve import Candidate, resolve
from ..models import Company, Person
from .base import LicenseRecord


def _get(row: dict[str, str], *keys: str) -> str | None:
    for k in keys:
        for actual in row:
            if actual.strip().lower() == k:
                v = (row[actual] or "").strip()
                return v or None
    return None


def parse_sfm_roster(csv_text: str) -> Iterator[LicenseRecord]:
    """Header-based CSV: business_name, license_number, license_class, status,
    qualifier_name, phone, address, city, state."""
    for row in csv.DictReader(csv_text.splitlines()):
        name = _get(row, "business_name", "business", "company", "name")
        if not name:
            continue
        yield LicenseRecord(
            source="sfm",
            licensee_name=_get(row, "qualifier_name", "qualifier") or name,
            business_name=name,
            license_type=_get(row, "license_class", "class", "license_type", "type"),
            license_number=_get(row, "license_number", "license", "number"),
            status_code=_get(row, "status"),
            phone=_get(row, "phone"),
            address=_get(row, "address"),
            city=_get(row, "city"),
            state=_get(row, "state"),
            is_business=True,
            raw={"qualifier": _get(row, "qualifier_name", "qualifier")},
        )


@dataclass
class SFMStats:
    firms_flagged: int = 0
    companies_created: int = 0
    notes: list[str] = field(default_factory=list)


def apply_sfm_records(session: Session, records: Iterable[LicenseRecord]) -> SFMStats:
    """Set ``is_fp_contractor`` on firms from SFM records (creating companies as
    needed via resolution). Registers the qualifier as a person when present."""
    stats = SFMStats()
    companies = list(session.exec(select(Company)).all())

    for rec in records:
        candidate = Candidate(
            name=rec.business_name or rec.licensee_name,
            license_number=rec.license_number,
            phone=rec.phone,
            address=rec.address,
        )
        result = resolve(candidate, companies)
        if result.company is not None and result.action in (
            "exact_license",
            "exact_name",
            "fuzzy_auto",
        ):
            company = result.company
        else:
            company = Company(
                canonical_name=rec.business_name or rec.licensee_name,
                normalized_key=normalize_company(rec.business_name or rec.licensee_name),
                phone=rec.phone,
                address=rec.address,
            )
            session.add(company)
            session.flush()
            companies.append(company)
            stats.companies_created += 1

        company.is_fp_contractor = True
        if rec.license_number and rec.license_number not in (company.license_numbers or []):
            company.license_numbers = [*(company.license_numbers or []), rec.license_number]
        stats.firms_flagged += 1

        qualifier = (rec.raw or {}).get("qualifier")
        if qualifier:
            exists = session.exec(
                select(Person).where(Person.name == qualifier, Person.company_id == company.id)
            ).first()
            if exists is None:
                session.add(
                    Person(
                        name=qualifier,
                        license_number=rec.license_number,
                        license_type=rec.license_type,
                        discipline="Fire Protection Contractor",
                        status=rec.status_code,
                        company_id=company.id,
                    )
                )
    session.commit()
    return stats
