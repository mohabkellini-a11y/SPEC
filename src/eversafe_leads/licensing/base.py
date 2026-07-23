"""Shared types for licensing collectors (Module 3)."""

from __future__ import annotations

from datetime import date
from typing import Any

from pydantic import BaseModel, ConfigDict


class LicenseRecord(BaseModel):
    """One licensee row from a licensing source, source-agnostic.

    ``raw`` always carries the untouched source row so nothing is lost even when
    a column's meaning is not yet confirmed against the source's layout doc.
    """

    model_config = ConfigDict(extra="forbid")

    source: str  # dbpr | sfm | fbpe
    licensee_name: str
    business_name: str | None = None
    license_type: str | None = None  # class code, e.g. CBC, CGC, EC, PE
    license_number: str | None = None  # canonical full number, e.g. CBC015061
    discipline: str | None = None  # e.g. "Fire Protection", "Civil" (FBPE PEs)
    status_code: str | None = None
    address: str | None = None
    city: str | None = None
    state: str | None = None
    postal_code: str | None = None
    county_code: str | None = None
    phone: str | None = None
    original_date: date | None = None
    expiry_date: date | None = None
    is_business: bool = False
    raw: dict[str, Any] = {}
