"""Lake County — custom "Permit Activity Reports" adapter (Module 1).

Verified live 2026-07-22/23: robots allows ``building_services/``. The
``permits_issued.aspx`` page is ASP.NET WebForms — the grid only appears after a
``__VIEWSTATE`` postback carrying a date range. The response groups permits under
a type header, then lists each permit as two cells:

    "2026031318 ISSUED"
    "177  TOWN CENTER BV / CLERMONT INSTALLATION OF FIRE ALARM ..."

Fire work is native as permit types: ``FIRE SPRINKLER - COMMERCIAL``,
``FIRE ALARM SYSTEM - COMMERCIAL``, ``FIRE MAIN UNDERGROUND DEDICATED -
COMMERCIAL``, ``FIRE SUPPRESSION SYSTEMS``. Individual rows carry no per-permit
date (the date range is the query), so ``issued_date`` is left null — the window
is recorded in ``raw_payload`` rather than a fabricated date (SPEC §2).
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import date
from typing import Any

from ..http import PoliteClient
from ..models import PermitDetail, PermitStub
from .base import JurisdictionAdapter

REPORT_PAGE = "permits_issued.aspx"

FIRE_TOKENS = ("FIRE", "SPRINKLER", "ALARM", "SUPPRESSION", "STANDPIPE")
_PERMIT_ROW = re.compile(r"^(\d{6,})\s+([A-Za-z/]+)$")
_TD = re.compile(r"<td[^>]*>(.*?)</td>", re.S | re.I)
_TAG = re.compile(r"<[^>]+>")
_HIDDEN = re.compile(r'name="(__[A-Z]+)"[^>]*value="([^"]*)"')


def _clean_cell(raw: str) -> str:
    text = _TAG.sub(" ", raw).replace("\xa0", " ").replace("&nbsp;", " ")
    return re.sub(r"\s+", " ", text).strip()


def _is_fire(type_label: str) -> bool:
    up = type_label.upper()
    return any(tok in up for tok in FIRE_TOKENS)


def parse_report(html: str) -> list[dict[str, Any]]:
    """Parse a ``permits_issued`` postback into permit dicts (commercial + fire,
    residential excluded). Pure function over the HTML — testable offline."""
    cells = [c for c in (_clean_cell(x) for x in _TD.findall(html)) if c]
    out: list[dict[str, Any]] = []
    current_type: str | None = None
    i = 0
    n = len(cells)
    while i < n:
        cell = cells[i]
        # A type header is the cell immediately before an "Issued: N" cell.
        if i + 1 < n and cells[i + 1].startswith("Issued:"):
            current_type = cell
            i += 2  # skip header + "Issued:"; the "Value:" cell is skipped by the loop
            continue
        m = _PERMIT_ROW.match(cell)
        if m and current_type and i + 1 < n:
            permit_no, status = m.group(1), m.group(2)
            detail = cells[i + 1]
            addr, _, rest = detail.partition(" / ")
            city, desc = "", ""
            if rest:
                parts = rest.split(" ", 1)
                city = parts[0]
                desc = parts[1] if len(parts) > 1 else ""
            up = current_type.upper()
            if "RESIDENTIAL" not in up and ("COMMERCIAL" in up or _is_fire(current_type)):
                out.append(
                    {
                        "record_number": permit_no,
                        "status": status,
                        "record_type": current_type,
                        "address_line1": addr.strip(),
                        "city": city.strip().title() or None,
                        "description": desc.strip() or None,
                        "is_fire": _is_fire(current_type),
                    }
                )
            i += 2
            continue
        i += 1
    return out


class LakeReportsAdapter(JurisdictionAdapter):
    platform = "lake_reports"

    def __init__(self, slug: str, config: dict[str, Any], client: PoliteClient | None = None):
        super().__init__(slug, config)
        self.report_url = self.base_url.rstrip("/") + "/" + REPORT_PAGE
        rl = config.get("rate_limit", {})
        self._own_client = client is None
        self.client = client or PoliteClient(
            min_interval=rl.get("min_interval_seconds", 2.0),
            jitter=rl.get("jitter_seconds", 1.0),
        )

    def _extract_hidden(self, html: str) -> dict[str, str]:
        return dict(_HIDDEN.findall(html))

    def search(
        self, since: date, until: date, record_types: list[str] | None = None
    ) -> Iterator[PermitStub]:
        page = self.client.get(self.report_url).text
        form = self._extract_hidden(page)
        form.update(
            {
                "__EVENTTARGET": "",
                "__EVENTARGUMENT": "",
                "lbPermitTypes": "All",
                "lbCities": "All",
                "txtStartDate": since.strftime("%m/%d/%Y"),
                "txtEndDate": until.strftime("%m/%d/%Y"),
                "rblDetails": "2",
                "btnSubmit": "Search Now",
            }
        )
        result_html = self.client.post(self.report_url, data=form).text
        window = {"since": since.isoformat(), "until": until.isoformat()}
        for rec in parse_report(result_html):
            rec = {**rec, "query_window": window}
            yield PermitStub(
                record_number=rec["record_number"],
                record_type=rec["record_type"],
                status=rec["status"],
                address_line1=rec["address_line1"],
                portal_url=self.report_url,
                source_url=self.report_url,
                raw_payload=rec,
            )

    def fetch_detail(self, stub: PermitStub) -> PermitDetail:
        rec = stub.raw_payload
        return PermitDetail(
            record_number=stub.record_number,
            record_type=rec.get("record_type"),
            record_subtype="fire" if rec.get("is_fire") else "commercial",
            description=rec.get("description"),
            status=rec.get("status"),
            address_line1=rec.get("address_line1"),
            city=rec.get("city"),
            state="FL",
            portal_url=self.report_url,
            source_url=stub.source_url,
            raw_payload=rec,
        )

    def is_fire_related(self, detail: PermitDetail) -> bool:
        return bool(detail.record_type and _is_fire(detail.record_type))

    def close(self) -> None:
        if self._own_client:
            self.client.close()
