"""Lake County permits_issued parser + adapter."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from eversafe_leads.adapters.lake_reports import LakeReportsAdapter, parse_report
from eversafe_leads.models import PermitDetail, PermitStub

FIXTURES = Path(__file__).parent / "fixtures"
HTML = (FIXTURES / "lake_permits_issued.html").read_text()

CONFIG = {
    "platform": "lake_reports",
    "display_name": "Lake County",
    "base_url": "https://c.lakecountyfl.gov/offices/building_services/permit_activity_reports/",
    "rate_limit": {"min_interval_seconds": 0, "jitter_seconds": 0},
}


def test_parse_report_filters_and_fields():
    rows = parse_report(HTML)
    nums = {r["record_number"] for r in rows}
    # commercial + fire kept; residential dropped
    assert "2026031318" in nums  # fire alarm commercial
    assert "2026055012" in nums  # fire sprinkler commercial
    assert "2026070777" in nums  # addition commercial
    assert "2026070999" not in nums  # fire sprinkler RESIDENTIAL -> excluded
    assert "2026070500" not in nums  # addition residential -> excluded

    alarm = next(r for r in rows if r["record_number"] == "2026031318")
    assert alarm["record_type"] == "FIRE ALARM SYSTEM - COMMERCIAL"
    assert alarm["is_fire"] is True
    assert alarm["address_line1"] == "177 TOWN CENTER BV"
    assert alarm["city"] == "Clermont"
    assert "FIRE ALARM" in alarm["description"]

    addition = next(r for r in rows if r["record_number"] == "2026070777")
    assert addition["is_fire"] is False


def test_adapter_fetch_detail():
    adapter = LakeReportsAdapter("lake", CONFIG)
    rows = parse_report(HTML)
    rec = next(r for r in rows if r["record_number"] == "2026055012")
    stub = PermitStub(
        record_number=rec["record_number"],
        record_type=rec["record_type"],
        status=rec["status"],
        source_url="https://x/permits_issued.aspx",
        raw_payload=rec,
    )
    detail = adapter.fetch_detail(stub)
    assert isinstance(detail, PermitDetail)
    assert detail.record_number == "2026055012"
    assert detail.record_subtype == "fire"
    assert detail.city == "Clermont"
    assert detail.state == "FL"
    assert adapter.is_fire_related(detail)


def test_search_via_mocked_client():
    """search() does a GET (for VIEWSTATE) then POST; mock both."""
    import httpx
    import respx

    with respx.mock:
        url = CONFIG["base_url"] + "permits_issued.aspx"
        respx.get(url).mock(
            return_value=httpx.Response(
                200,
                text='<input name="__VIEWSTATE" value="vs"/><input name="__EVENTVALIDATION" value="ev"/>',
            )
        )
        respx.post(url).mock(return_value=httpx.Response(200, text=HTML))
        adapter = LakeReportsAdapter("lake", CONFIG)
        stubs = list(adapter.search(date(2026, 7, 1), date(2026, 7, 23)))

    nums = {s.record_number for s in stubs}
    assert "2026055012" in nums and "2026070999" not in nums
    assert all(isinstance(s, PermitStub) for s in stubs)
