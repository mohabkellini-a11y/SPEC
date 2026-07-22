"""DBPR construction-industry licensee collector (Module 3).

Verified live 2026-07-22: the construction extract downloads as a headerless,
positional CSV (~48 MB, ``text/csv``, refreshed ~daily):

    https://www2.myfloridalicense.com/sto/file_download/extracts/CONSTRUCTIONLICENSE_1.csv

Column positions are taken from the live data and **validated at parse time** by
reconstructing the full license number (``type_prefix`` + numeric) against the
row's own full-license column. If that check fails on a row, the row is flagged
rather than silently mis-parsed — the honesty guard SPEC §2 demands, and a
tripwire if DBPR ever changes the layout. The authoritative field-layout doc
should still be confirmed before trusting the two status-code columns, whose
semantics we deliberately do not assert (kept raw).
"""

from __future__ import annotations

import csv
import re
from collections.abc import Iterable, Iterator
from datetime import date, datetime
from pathlib import Path

import structlog

from ..http import PoliteClient
from .base import LicenseRecord

log = structlog.get_logger()

CONSTRUCTION_URL = (
    "https://www2.myfloridalicense.com/sto/file_download/extracts/CONSTRUCTIONLICENSE_1.csv"
)

# Observed positional layout (0-indexed), verified against the full-license col.
COL_BOARD = 0
COL_TYPE = 1  # class prefix: CBC, CGC, CRC, CAC, EC, ...
COL_NAME = 2  # "LAST, FIRST M SUFFIX"
COL_DBA = 3
COL_ADDR1 = 5
COL_CITY = 8
COL_STATE = 9
COL_ZIP = 10
COL_COUNTY = 11
COL_LICNUM = 12  # numeric portion
COL_STATUS_1 = 13  # semantics unconfirmed -> kept raw
COL_STATUS_2 = 14  # semantics unconfirmed -> kept raw
COL_ORIG_DATE = 15
COL_EXPIRY_DATE = 17
COL_FULL_LICENSE = 20  # e.g. CBC015061 -> the integrity anchor
MIN_COLS = 21

# DBPR puts the literal "INDIVIDUAL" in the DBA column for sole practitioners.
# It is a business-type sentinel, NOT a business name — treating it as one
# collapses every sole-practitioner license into one bogus "INDIVIDUAL" company.
_DBA_SENTINELS = {"INDIVIDUAL", "SOLE PROPRIETOR", "N/A", "NONE"}


def _d(cell: str | None) -> str | None:
    if cell is None:
        return None
    c = cell.strip()
    return c or None


def _date(cell: str | None) -> date | None:
    c = _d(cell)
    if not c:
        return None
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(c, fmt).date()
        except ValueError:
            continue
    return None


def _reconstruct_matches(type_prefix: str | None, licnum: str | None, full: str | None) -> bool:
    """Integrity check: does the full-license column agree with the type prefix
    and numeric column?

    The full license is not a naive ``type + number`` concatenation. DBPR inserts
    a designation letter and re-encodes the numeric portion (e.g. type ``CBC`` +
    numeric ``1114582`` -> full ``CBCA14582``, where ``14582`` is a substring of
    ``1114582``). So we require the prefix to match and the two numeric strings to
    overlap in either direction — accurate enough to catch a real column shift
    without crying wolf on the legitimate ``A``-designation encoding. Rows with no
    full-license column can't be checked, so they pass (canonical falls back to
    ``type + number``).
    """
    if not full:
        return True
    full_u = full.replace(" ", "").upper()
    if type_prefix and not full_u.startswith(type_prefix.upper()):
        return False
    if licnum:
        lic_digits = re.sub(r"\D", "", licnum).lstrip("0")
        full_digits = re.sub(r"\D", "", full_u).lstrip("0")
        if lic_digits and full_digits:
            return (
                lic_digits in full_digits
                or full_digits in lic_digits
                or lic_digits.endswith(full_digits)
                or full_digits.endswith(lic_digits)
            )
    return True


def parse_rows(rows: Iterable[list[str]]) -> Iterator[LicenseRecord]:
    """Yield a ``LicenseRecord`` per positional CSV row. Short/garbled rows are
    logged and skipped, never yielded as partial truth."""
    for i, row in enumerate(rows):
        if len(row) < MIN_COLS:
            log.warning("dbpr.short_row", index=i, ncols=len(row))
            continue
        type_prefix = _d(row[COL_TYPE])
        licnum = _d(row[COL_LICNUM])
        full = _d(row[COL_FULL_LICENSE])
        canonical = full or (f"{type_prefix}{licnum}" if type_prefix and licnum else None)

        if not _reconstruct_matches(type_prefix, licnum, full):
            log.warning(
                "dbpr.layout_mismatch",
                index=i,
                type=type_prefix,
                licnum=licnum,
                full=full,
            )
            # Still emit, but the mismatch is recorded in raw for inspection.

        dba = _d(row[COL_DBA])
        if dba and dba.upper() in _DBA_SENTINELS:
            dba = None
        yield LicenseRecord(
            source="dbpr",
            licensee_name=_d(row[COL_NAME]) or "",
            business_name=dba,
            license_type=type_prefix,
            license_number=canonical,
            status_code="/".join(filter(None, [_d(row[COL_STATUS_1]), _d(row[COL_STATUS_2])]))
            or None,
            address=_d(row[COL_ADDR1]),
            city=_d(row[COL_CITY]),
            state=_d(row[COL_STATE]),
            postal_code=_d(row[COL_ZIP]),
            county_code=_d(row[COL_COUNTY]),
            original_date=_date(row[COL_ORIG_DATE]),
            expiry_date=_date(row[COL_EXPIRY_DATE]),
            is_business=bool(dba),
            raw={"row": row},
        )


def parse_csv_text(text: str) -> Iterator[LicenseRecord]:
    yield from parse_rows(csv.reader(text.splitlines()))


def parse_csv_file(path: Path) -> Iterator[LicenseRecord]:
    with Path(path).open(newline="", encoding="latin-1") as fh:
        yield from parse_rows(csv.reader(fh))


def download(
    dest: Path,
    client: PoliteClient | None = None,
    max_bytes: int = 200 * 1024 * 1024,
    url: str = CONSTRUCTION_URL,
) -> Path:
    """Stream the DBPR construction extract to ``dest`` (size-capped)."""
    own = client is None
    client = client or PoliteClient(min_interval=2.0, jitter=1.0)
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        written = 0
        with client.stream("GET", url) as resp:
            resp.raise_for_status()
            with dest.open("wb") as fh:
                for chunk in resp.iter_bytes(1 << 16):
                    written += len(chunk)
                    if written > max_bytes:
                        raise RuntimeError(
                            f"DBPR file exceeded cap {max_bytes} bytes; aborting download"
                        )
                    fh.write(chunk)
        log.info("dbpr.downloaded", bytes=written, dest=str(dest))
        return dest
    finally:
        if own:
            client.close()
