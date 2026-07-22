"""City of Orlando — Socrata SODA adapter (Module 1).

Verified live 2026-07-22 against dataset ``ryhf-m453`` ("Permit Applications").
The whole record ships in one SODA row, so ``fetch_detail`` re-parses the row
captured by ``search`` rather than making a second request.

Quirks (see CLAUDE.md): filter date column is ``processed_date`` (there is no
``application_date``); fire work is native in ``worktype`` (``FireSupp``, ``FA``);
review comment text is NOT in this dataset — only ``of_cycles`` and
``under_review_date`` are available for Module 2.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import date, datetime
from typing import Any
from urllib.parse import quote

from ..http import PoliteClient
from ..models import (
    PartyRole,
    PartyStub,
    PermitDetail,
    PermitStub,
)
from .base import JurisdictionAdapter

# SPEC §6 fire/life-safety description matcher (used as a secondary include).
FIRE_DESCRIPTION_TOKENS = (
    "sprinkler",
    "fire alarm",
    "fire suppression",
    "standpipe",
    "hood",
    "clean agent",
    "facp",
    "nfpa",
)


def _parse_date(value: Any) -> date | None:
    if not value:
        return None
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    # Socrata floating timestamps: '2026-07-22T00:00:00.000'
    try:
        return datetime.fromisoformat(text.replace("Z", "")).date()
    except ValueError:
        try:
            return datetime.strptime(text[:10], "%Y-%m-%d").date()
        except ValueError:
            return None


def _parse_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_int(value: Any) -> int | None:
    f = _parse_float(value)
    return int(f) if f is not None else None


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


class SocrataAdapter(JurisdictionAdapter):
    platform = "socrata"

    def __init__(self, slug: str, config: dict[str, Any], client: PoliteClient | None = None):
        super().__init__(slug, config)
        soc = config.get("socrata", {})
        self.dataset_id: str = soc["dataset_id"]
        self.date_field: str = soc.get("date_field", "processed_date")
        self.fire_worktypes: list[str] = soc.get("fire_worktypes", ["FireSupp", "FA"])
        self.exclude_worktypes: list[str] = soc.get("exclude_worktypes", [])
        self.resource_url = f"{self.base_url}/resource/{self.dataset_id}.json"

        rl = config.get("rate_limit", {})
        self._own_client = client is None
        self.client = client or PoliteClient(
            min_interval=rl.get("min_interval_seconds", 1.0),
            jitter=rl.get("jitter_seconds", 0.5),
        )
        # Optional SODA app token for higher throughput (never required).
        self.app_token = os.environ.get("SOCRATA_APP_TOKEN")

    # -- helpers ----------------------------------------------------------- #
    def _build_url(self, extra: dict[str, str]) -> str:
        """Build the request URL to satisfy two WAF quirks (see CLAUDE.md).

        The WAF fronting Orlando's Socrata rejects (403) a complex ``$where``
        unless BOTH hold:

        1. Spaces are ``%20``-encoded, not ``+`` (a ``+`` between quoted
           literals reads as SQL-injection-like to the WAF).
        2. The ``$`` in SoQL param names stays **literal** — ``$where``, not the
           ``%24where`` that ``urlencode`` would produce — or the request 403s.

        So we join keys with a literal ``$`` and percent-encode only the values
        with ``quote`` (``safe=''`` → spaces become ``%20``).
        """
        params = dict(extra)
        if self.app_token:
            params["$$app_token"] = self.app_token
        qs = "&".join(f"{key}={quote(value, safe='')}" for key, value in params.items())
        return f"{self.resource_url}?{qs}"

    def _commercial_where(self, since: date, until: date) -> str:
        fire_list = ",".join(f"'{w}'" for w in self.fire_worktypes)
        # A permit qualifies if it is native fire work, any Fire Permit, or
        # commercial plan-review work that is not on the exclusion list
        # (roofing/signs/fences/pools per SPEC §6). Fire always wins.
        commercial_branch = "plan_review_type = 'Commercial'"
        if self.exclude_worktypes:
            excl = ",".join(f"'{w}'" for w in self.exclude_worktypes)
            commercial_branch += f" AND worktype not in ({excl})"
        include = (
            f"(worktype in ({fire_list}) "
            "OR application_type = 'Fire Permit' "
            f"OR ({commercial_branch}))"
        )
        window = (
            f"{self.date_field} >= '{since.isoformat()}T00:00:00' "
            f"AND {self.date_field} <= '{until.isoformat()}T23:59:59'"
        )
        return f"{window} AND {include}"

    def _row_source_url(self, record_number: str) -> str:
        return f"{self.resource_url}?permit_number={record_number}"

    # -- interface --------------------------------------------------------- #
    def search(
        self, since: date, until: date, record_types: list[str] | None = None
    ) -> Iterator[PermitStub]:
        where = self._commercial_where(since, until)
        page_size = 1000
        offset = 0
        while True:
            url = self._build_url(
                {
                    "$where": where,
                    "$order": f"{self.date_field} DESC",
                    "$limit": str(page_size),
                    "$offset": str(offset),
                }
            )
            rows: list[dict[str, Any]] = self.client.get_json(url)
            if not rows:
                break
            for row in rows:
                record_number = _clean(row.get("permit_number"))
                if not record_number:
                    continue
                yield PermitStub(
                    record_number=record_number,
                    record_type=_clean(row.get("application_type")),
                    status=_clean(row.get("application_status")),
                    applied_date=_parse_date(row.get(self.date_field)),
                    address_line1=_clean(row.get("permit_address")),
                    portal_url=self.base_url,
                    source_url=self._row_source_url(record_number),
                    raw_payload=row,
                )
            if len(rows) < page_size:
                break
            offset += page_size

    def fetch_detail(self, stub: PermitStub) -> PermitDetail:
        row = stub.raw_payload
        geo = row.get("geocoded_column") or {}
        coords = geo.get("coordinates") if isinstance(geo, dict) else None
        lon, lat = (coords + [None, None])[:2] if coords else (None, None)

        parties: list[PartyStub] = []
        contractor_name = _clean(row.get("contractor_name")) or _clean(row.get("contractor"))
        if contractor_name:
            parties.append(
                PartyStub(
                    role=PartyRole.contractor,
                    raw_name=contractor_name,
                    phone=_clean(row.get("contractor_phone_number")),
                    address=_clean(row.get("contractor_address")),
                )
            )
        owner_name = _clean(row.get("property_owner_name")) or _clean(row.get("parcel_owner_name"))
        if owner_name:
            parties.append(PartyStub(role=PartyRole.owner, raw_name=owner_name))
        provider = _clean(row.get("private_provider_company_name"))
        if provider:
            parties.append(PartyStub(role=PartyRole.agent, raw_name=provider))

        description = _clean(row.get("project_name")) or _clean(row.get("location"))

        return PermitDetail(
            record_number=stub.record_number,
            record_type=_clean(row.get("application_type")),
            record_subtype=_clean(row.get("worktype")),
            description=description,
            status=_clean(row.get("application_status")),
            applied_date=_parse_date(row.get(self.date_field)),
            issued_date=_parse_date(row.get("issue_permit_date")),
            finaled_date=_parse_date(row.get("final_date")),
            valuation=_parse_float(row.get("estimated_cost")),
            square_footage=_parse_float(row.get("square_footage")),
            occupancy_type=_clean(row.get("plan_review_type")),
            address_line1=_clean(row.get("permit_address")),
            city="Orlando",
            state="FL",
            postal_code=None,
            parcel_id=_clean(row.get("parcel_number")),
            lat=_parse_float(lat),
            lon=_parse_float(lon),
            portal_url=self.base_url,
            review_cycles=_parse_int(row.get("of_cycles")),
            source_url=stub.source_url,
            raw_payload=row,
            parties=parties,
        )

    def is_fire_related(self, detail: PermitDetail) -> bool:
        """True if this permit is fire/life-safety work (worktype or description)."""
        if detail.record_subtype in self.fire_worktypes:
            return True
        if detail.record_type and "fire" in detail.record_type.lower():
            return True
        haystack = " ".join(filter(None, [detail.description, detail.record_type])).lower()
        return any(tok in haystack for tok in FIRE_DESCRIPTION_TOKENS)

    def close(self) -> None:
        if self._own_client:
            self.client.close()
