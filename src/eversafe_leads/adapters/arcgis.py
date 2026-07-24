"""Generic ArcGIS FeatureServer / MapServer adapter (Module 1).

ArcGIS is the cheap-path jackpot when a county publishes permits as a REST layer
(SPEC §6). This adapter is fully config-driven: point it at a layer, name the
date/type/record fields and the commercial + fire type codes, and it paginates
the REST `query` endpoint into permits — no per-county code.

Verified live 2026-07-24 against Volusia County ("GRM's AMANDA OPEN Permits",
`maps5.vcgov.org/.../CurrentProjects/MapServer/1`): native `FIRE` and `COM`
folder types, `INDATE` date field, `REFERENCEFILE` record number.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import UTC, date, datetime
from typing import Any
from urllib.parse import quote

from ..http import PoliteClient
from ..models import PermitDetail, PermitStub
from .base import JurisdictionAdapter

FIRE_DESCRIPTION_TOKENS = (
    "sprinkler",
    "fire alarm",
    "fire suppression",
    "standpipe",
    "fire pump",
    "fire supply",
    "nfpa",
    "fire main",
    "clean agent",
)
_PAGE = 1000
_ZIP = re.compile(r"\b(\d{5})(?:-\d{4})?\b")


def _epoch_ms_to_date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=UTC).date()
    except (TypeError, ValueError, OSError):
        return None


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _split_address(folder_name: str | None) -> tuple[str | None, str | None, str | None]:
    """ "5300 S ATLANTIC Avenue, NEW SMYRNA BEACH 32169" -> (addr, city, zip)."""
    if not folder_name:
        return None, None, None
    addr, _, rest = folder_name.partition(",")
    rest = rest.strip()
    zip_m = _ZIP.search(rest)
    postal = zip_m.group(1) if zip_m else None
    city = rest[: zip_m.start()].strip() if zip_m else (rest or None)
    return addr.strip() or None, (city or None), postal


class ArcgisAdapter(JurisdictionAdapter):
    platform = "arcgis"

    def __init__(self, slug: str, config: dict[str, Any], client: PoliteClient | None = None):
        super().__init__(slug, config)
        ag = config.get("arcgis", {})
        self.layer_url: str = ag["layer_url"].rstrip("/")
        self.date_field: str = ag["date_field"]
        self.record_field: str = ag["record_field"]
        self.type_field: str = ag.get("type_field", "")
        self.fire_types: list[str] = ag.get("fire_types", [])
        self.commercial_types: list[str] = ag.get("commercial_types", [])
        self.fmap: dict[str, str] = ag.get("field_map", {})

        rl = config.get("rate_limit", {})
        self._own_client = client is None
        self.client = client or PoliteClient(
            min_interval=rl.get("min_interval_seconds", 1.0), jitter=rl.get("jitter_seconds", 0.5)
        )

    def _where(self, since: date, until: date) -> str:
        clauses = [
            f"{self.date_field} >= timestamp '{since.isoformat()} 00:00:00'",
            f"{self.date_field} <= timestamp '{until.isoformat()} 23:59:59'",
        ]
        types = [*self.fire_types, *self.commercial_types]
        if self.type_field and types:
            in_list = ",".join(f"'{t}'" for t in types)
            clauses.append(f"{self.type_field} IN ({in_list})")
        return " AND ".join(clauses)

    def _query_url(self, where: str, offset: int) -> str:
        params = {
            "where": where,
            "outFields": "*",
            "returnGeometry": "false",
            "orderByFields": f"{self.date_field} DESC",
            "resultOffset": str(offset),
            "resultRecordCount": str(_PAGE),
            "f": "json",
        }
        qs = "&".join(f"{k}={quote(v, safe='')}" for k, v in params.items())
        return f"{self.layer_url}/query?{qs}"

    def search(
        self, since: date, until: date, record_types: list[str] | None = None
    ) -> Iterator[PermitStub]:
        where = self._where(since, until)
        offset = 0
        while True:
            data = self.client.get_json(self._query_url(where, offset))
            feats = data.get("features", [])
            if not feats:
                break
            for feat in feats:
                attrs = feat.get("attributes", {})
                record_number = _clean(attrs.get(self.record_field))
                if not record_number:
                    continue
                yield PermitStub(
                    record_number=record_number,
                    record_type=_clean(attrs.get(self.type_field)) if self.type_field else None,
                    status=_clean(attrs.get(self.fmap.get("status", ""))),
                    applied_date=_epoch_ms_to_date(attrs.get(self.date_field)),
                    portal_url=_clean(attrs.get(self.fmap.get("portal_url", ""))),
                    source_url=f"{self.layer_url}/query?{self.record_field}={record_number}",
                    raw_payload=attrs,
                )
            if len(feats) < _PAGE or not data.get("exceededTransferLimit"):
                break
            offset += _PAGE

    def fetch_detail(self, stub: PermitStub) -> PermitDetail:
        a = stub.raw_payload
        addr, city, postal = _split_address(_clean(a.get(self.fmap.get("address", ""))))
        subtype = _clean(a.get(self.type_field)) if self.type_field else None
        return PermitDetail(
            record_number=stub.record_number,
            record_type=_clean(a.get(self.fmap.get("type_label", ""))) or subtype,
            record_subtype=subtype,
            description=_clean(a.get(self.fmap.get("description", ""))),
            status=_clean(a.get(self.fmap.get("status", ""))),
            applied_date=_epoch_ms_to_date(a.get(self.date_field)),
            address_line1=addr,
            city=city,
            state="FL",
            postal_code=postal,
            parcel_id=_clean(a.get(self.fmap.get("parcel_id", ""))),
            portal_url=_clean(a.get(self.fmap.get("portal_url", ""))),
            source_url=stub.source_url,
            raw_payload=a,
        )

    def is_fire_related(self, detail: PermitDetail) -> bool:
        if detail.record_subtype in self.fire_types:
            return True
        haystack = " ".join(filter(None, [detail.description, detail.record_type])).lower()
        return any(tok in haystack for tok in FIRE_DESCRIPTION_TOKENS)

    def close(self) -> None:
        if self._own_client:
            self.client.close()
