"""Generic ArcGIS adapter (Volusia config)."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import httpx
import respx

from eversafe_leads.adapters.arcgis import ArcgisAdapter, _split_address
from eversafe_leads.models import PermitDetail, PermitStub

FIXTURES = Path(__file__).parent / "fixtures"
LAYER = "https://maps5.vcgov.org/arcgis/rest/services/CurrentProjects/MapServer/1"

CONFIG = {
    "platform": "arcgis",
    "display_name": "Volusia County",
    "base_url": LAYER,
    "rate_limit": {"min_interval_seconds": 0, "jitter_seconds": 0},
    "arcgis": {
        "layer_url": LAYER,
        "date_field": "INDATE",
        "record_field": "REFERENCEFILE",
        "type_field": "FOLDERTYPE",
        "fire_types": ["FIRE"],
        "commercial_types": ["COM"],
        "field_map": {
            "address": "FOLDERNAME",
            "description": "FOLDERDESCRIPTION",
            "status": "STATUSDESC",
            "parcel_id": "PID",
            "portal_url": "FOLDERLINK",
            "type_label": "folderdesc",
        },
    },
}


def test_split_address():
    assert _split_address("2360 OCEAN SHORE Boulevard, ORMOND BEACH 32176") == (
        "2360 OCEAN SHORE Boulevard",
        "ORMOND BEACH",
        "32176",
    )
    assert _split_address(None) == (None, None, None)


def test_where_clause_types_and_dates():
    a = ArcgisAdapter("volusia", CONFIG)
    where = a._where(date(2026, 1, 1), date(2026, 7, 24))
    assert "INDATE >= timestamp '2026-01-01 00:00:00'" in where
    assert "INDATE <= timestamp '2026-07-24 23:59:59'" in where
    assert "FOLDERTYPE IN ('FIRE','COM')" in where


def test_fetch_detail_and_fire_flag():
    a = ArcgisAdapter("volusia", CONFIG)
    rows = json.loads((FIXTURES / "volusia_arcgis.json").read_text())["features"]
    fire = rows[0]["attributes"]
    stub = PermitStub(record_number=fire["REFERENCEFILE"], source_url="x", raw_payload=fire)
    detail = a.fetch_detail(stub)
    assert isinstance(detail, PermitDetail)
    assert detail.record_number == "20260429058"
    assert detail.record_subtype == "FIRE"
    assert detail.record_type == "Fire Permit"
    assert detail.address_line1 == "2360 OCEAN SHORE Boulevard"
    assert detail.city == "ORMOND BEACH"
    assert detail.postal_code == "32176"
    assert detail.applied_date == date(2026, 5, 3)
    assert a.is_fire_related(detail)  # FIRE type

    com = rows[1]["attributes"]
    com_detail = a.fetch_detail(PermitStub(record_number="x", source_url="x", raw_payload=com))
    assert not a.is_fire_related(com_detail)  # commercial stucco repair, not fire


@respx.mock
def test_search_paginates():
    a = ArcgisAdapter("volusia", CONFIG)
    rows = json.loads((FIXTURES / "volusia_arcgis.json").read_text())
    respx.get(url__startswith=f"{LAYER}/query").mock(
        side_effect=[httpx.Response(200, json=rows), httpx.Response(200, json={"features": []})]
    )
    stubs = list(a.search(date(2026, 1, 1), date(2026, 7, 24)))
    assert {s.record_number for s in stubs} == {"20260429058", "20260428041"}
