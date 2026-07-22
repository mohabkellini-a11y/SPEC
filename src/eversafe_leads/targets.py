"""Target lists (SPEC §6, Module 3 analysis).

- **List A** — FP contractors with active licenses, no Certificate of
  Authorization, no FP PE on record, non-zero recent permit volume.
- **List B** — engineering/architecture firms with a CA and recent commercial
  permit volume, but no FP-discipline PE on record.
- **fire-active** — a permit-derived ranking that works today from Module 1 data
  alone: firms pulling the most fire-protection permits. It is the practical
  precursor to List A while the SFM/FBPE flags are being collected, and is
  labeled as permit-derived (not licensure-verified) so nothing is overstated.

Both A and B depend on the ``is_fp_contractor`` / ``has_engineering_ca`` /
``has_fp_pe_on_record`` flags, which are populated by the SFM and FBPE
collectors. Until those run, A and B are correctly *empty* — the function says
so rather than inventing rows.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import date

from sqlmodel import Session, select

from .enrich.companies import company_permit_stats
from .models import Company


@dataclass
class TargetRow:
    company: Company
    permit_count: int
    fire_permit_count: int
    reason: str


def _companies_by_id(session: Session) -> dict[int, Company]:
    return {c.id: c for c in session.exec(select(Company)).all()}


def target_list(
    session: Session,
    which: str,
    county_slug: str | None = None,
    months: int = 12,
    today: date | None = None,
    limit: int | None = None,
) -> list[TargetRow]:
    stats = company_permit_stats(session, county_slug=county_slug, months=months, today=today)
    companies = _companies_by_id(session)
    rows: list[TargetRow] = []

    for company_id, s in stats.items():
        company = companies.get(company_id)
        if company is None or s["permit_count"] == 0:
            continue
        which_l = which.lower()

        if which_l == "a":
            if (
                company.is_fp_contractor
                and not company.has_engineering_ca
                and not (company.has_fp_pe_on_record)
            ):
                rows.append(
                    TargetRow(
                        company,
                        s["permit_count"],
                        s["fire_permit_count"],
                        "FP contractor, no CA, no FP PE on record",
                    )
                )
        elif which_l == "b":
            if company.has_engineering_ca and not company.has_fp_pe_on_record:
                rows.append(
                    TargetRow(
                        company,
                        s["permit_count"],
                        s["fire_permit_count"],
                        "eng/arch firm with CA, no FP PE on record",
                    )
                )
        elif which_l == "fire-active":
            if s["fire_permit_count"] > 0:
                rows.append(
                    TargetRow(
                        company,
                        s["permit_count"],
                        s["fire_permit_count"],
                        f"pulled {s['fire_permit_count']} fire permits (permit-derived)",
                    )
                )
        else:
            raise ValueError(f"unknown target list '{which}' (use A, B, or fire-active)")

    # Sort by recent permit volume, descending (SPEC §6).
    rows.sort(key=lambda r: (r.fire_permit_count, r.permit_count), reverse=True)
    return rows[:limit] if limit else rows


def to_csv(rows: list[TargetRow]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["company", "phone", "licenses", "permit_count", "fire_permit_count", "reason"])
    for r in rows:
        writer.writerow(
            [
                r.company.canonical_name,
                r.company.phone or "",
                ";".join(r.company.license_numbers or []),
                r.permit_count,
                r.fire_permit_count,
                r.reason,
            ]
        )
    return buf.getvalue()
