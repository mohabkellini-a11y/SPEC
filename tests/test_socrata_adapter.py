"""Phase 1 acceptance: the saved fixture parses to the expected model."""

from __future__ import annotations

from datetime import date

import httpx
import respx

from eversafe_leads.adapters.socrata import SocrataAdapter, _parse_date, _parse_float
from eversafe_leads.models import PartyRole, PermitDetail, PermitStub


def _adapter(orlando_config) -> SocrataAdapter:
    return SocrataAdapter("orlando", orlando_config)


def test_fixture_row_parses_to_permit_detail(orlando_config, fire_rows):
    adapter = _adapter(orlando_config)
    row = next(r for r in fire_rows if r["permit_number"] == "FIR2026-11084")
    stub = PermitStub(
        record_number=row["permit_number"],
        source_url="https://example/resource?permit_number=FIR2026-11084",
        raw_payload=row,
    )

    detail = adapter.fetch_detail(stub)

    assert isinstance(detail, PermitDetail)
    assert detail.record_number == "FIR2026-11084"
    assert detail.record_type == "Fire Permit"
    assert detail.record_subtype == "FireSupp"
    assert detail.status == "Open"
    assert detail.applied_date == date(2026, 7, 22)
    assert detail.valuation == 148000.0
    assert detail.occupancy_type == "Commercial"
    assert detail.address_line1 == "2444 TRADEPORT DR"
    assert detail.city == "Orlando"
    assert detail.state == "FL"
    assert detail.parcel_id == "302407002600322"
    assert detail.review_cycles == 1

    # A contractor party is extracted with its phone.
    contractors = [p for p in detail.parties if p.role == PartyRole.contractor]
    assert len(contractors) == 1
    assert "WAYNE AUTOMATIC" in contractors[0].raw_name
    assert contractors[0].phone == "(407)656-3030"

    owners = [p for p in detail.parties if p.role == PartyRole.owner]
    assert owners and "MCLANE" in owners[0].raw_name


def test_is_fire_related(orlando_config, fire_rows):
    adapter = _adapter(orlando_config)
    for row in fire_rows:
        stub = PermitStub(record_number=row["permit_number"], source_url="x", raw_payload=row)
        assert adapter.is_fire_related(adapter.fetch_detail(stub))


def test_where_clause_excludes_and_includes(orlando_config):
    adapter = _adapter(orlando_config)
    where = adapter._commercial_where(date(2026, 6, 1), date(2026, 7, 22))
    assert "processed_date >= '2026-06-01T00:00:00'" in where
    assert "processed_date <= '2026-07-22T23:59:59'" in where
    assert "worktype in ('FireSupp','FA')" in where
    assert "application_type = 'Fire Permit'" in where
    assert "worktype not in ('Roof','Fence','Sign','Pool')" in where


def test_build_url_keeps_dollar_literal_and_percent20(orlando_config):
    """WAF quirk guard: $ stays literal, spaces are %20 (never +)."""
    adapter = _adapter(orlando_config)
    url = adapter._build_url({"$where": "a = 'b' AND c = 'd'", "$limit": "5"})
    assert "$where=" in url and "%24where" not in url
    assert "%20" in url and "+" not in url


@respx.mock
def test_search_paginates_and_yields_stubs(orlando_config, fire_rows):
    adapter = _adapter(orlando_config)
    route = respx.get("https://data.cityoforlando.net/resource/ryhf-m453.json")
    # First page returns the fixture rows; second page is empty → stop.
    route.side_effect = [
        httpx.Response(200, json=fire_rows),
        httpx.Response(200, json=[]),
    ]

    stubs = list(adapter.search(date(2026, 6, 1), date(2026, 7, 22)))

    assert len(stubs) == len(fire_rows)
    assert {s.record_number for s in stubs} == {r["permit_number"] for r in fire_rows}
    assert all(isinstance(s, PermitStub) for s in stubs)


def test_parse_helpers():
    assert _parse_date("2026-07-22T00:00:00.000") == date(2026, 7, 22)
    assert _parse_date("") is None
    assert _parse_float("148000") == 148000.0
    assert _parse_float("") is None
