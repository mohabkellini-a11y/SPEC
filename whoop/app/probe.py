"""Schema probe — point this at your real NOOP database and read the output.

Because NOOP's persistence layer is unpublished (SCHEMA_NOTES.md 0), this is how
you find out what the schema actually is on *your* install.

    python -m app.probe            # introspect NOOP_DB_PATH and report
    python -m app.probe --find     # search the usual install locations
    python -m app.probe --sql      # also dump the CREATE TABLE statements

Everything it does is read-only.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

from .config import settings
from .noop_adapter import NoopAdapter, NoopDBError

SEARCH_ROOTS = [
    "~/Library/Application Support",
    "~/Library/Containers",
    "~/.local/share",
    "~/.config",
    "~/AppData/Roaming",
    "~/AppData/Local",
]
DB_GLOBS = ("*.sqlite", "*.sqlite3", "*.db")


def find_candidates() -> list[Path]:
    """Look for NOOP-shaped SQLite files in the usual places."""
    hits: list[Path] = []
    for root in SEARCH_ROOTS:
        base = Path(root).expanduser()
        if not base.is_dir():
            continue
        for pattern in DB_GLOBS:
            try:
                for path in base.rglob(pattern):
                    parts = {p.lower() for p in path.parts}
                    if any("noop" in p or "strand" in p or "whoop" in p for p in parts):
                        hits.append(path)
            except (PermissionError, OSError):
                continue
    return sorted(set(hits))


def looks_like_noop(path: Path) -> tuple[bool, str]:
    """Cheap read-only sniff: does this file resolve as a NOOP store?"""
    try:
        adapter = NoopAdapter(path, None)
        smap = adapter.schema()
    except (NoopDBError, sqlite3.Error) as exc:
        return False, f"unreadable: {exc}"
    if smap.daily is not None:
        return True, f"daily metrics -> {smap.daily.table} ({len(smap.daily.columns)} fields)"
    return False, f"{len(smap.all_tables)} tables, no daily-metrics match"


def cmd_find() -> int:
    print("Searching for NOOP-shaped SQLite files...\n")
    candidates = find_candidates()
    if not candidates:
        print("Nothing found. NOOP may store its DB elsewhere on this machine.")
        print("Find it manually and set NOOP_DB_PATH in .env.")
        return 1
    for path in candidates:
        ok, detail = looks_like_noop(path)
        size = path.stat().st_size / 1e6
        print(f"  [{'MATCH' if ok else '  ?  '}] {path}  ({size:.1f} MB)")
        print(f"            {detail}")
    print("\nSet the winner as NOOP_DB_PATH in .env.")
    return 0


def cmd_probe(dump_sql: bool) -> int:
    path = settings.noop_db_path
    if path is None:
        print("NOOP_DB_PATH is not set. Copy .env.example to .env first.", file=sys.stderr)
        print("Then run `python -m app.probe --find` to locate the database.", file=sys.stderr)
        return 2

    print(f"NOOP database: {path}")
    if not path.is_file():
        print("  -> file does not exist", file=sys.stderr)
        return 2
    print(f"  size: {path.stat().st_size / 1e6:.1f} MB")
    for sidecar in ("-wal", "-shm"):
        side = path.with_name(path.name + sidecar)
        if side.exists():
            print(f"  sidecar present: {side.name} (NOOP is in WAL mode; must be readable)")
    print()

    adapter = NoopAdapter(path, settings.schema_map_path)
    try:
        smap = adapter.schema()
    except NoopDBError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(f"--- {len(smap.all_tables)} tables/views ---")
    for table in sorted(smap.all_tables):
        cols = smap.all_tables[table]
        print(f"  {table} ({len(cols)})")
        print(f"      {', '.join(cols)}")
    print()

    print("--- resolution ---")
    for label, match in (("daily metrics", smap.daily), ("sleep sessions", smap.sleep),
                         ("heart rate", smap.hr), ("skin temp", smap.skin_temp)):
        if match is None:
            print(f"  {label:<16} UNRESOLVED")
            continue
        print(f"  {label:<16} -> {match.table}")
        for canonical, actual in sorted(match.columns.items()):
            flag = "" if canonical == actual else "   (renamed)"
            print(f"        {canonical:<18} = {actual}{flag}")
        if match.missing:
            print(f"        missing: {', '.join(match.missing)}")
    if smap.battery:
        print(f"  {'battery':<16} -> {smap.battery[0]}.{smap.battery[1]}")
    else:
        print(f"  {'battery':<16} UNRESOLVED")
    print()

    if smap.overrides_applied:
        print(f"schema_map.json overrides applied to: {', '.join(smap.overrides_applied)}\n")

    if smap.notes:
        print("--- notes ---")
        for note in smap.notes:
            print(f"  * {note}")
        print()

    if smap.daily is not None:
        try:
            n = adapter.day_count()
            latest = adapter.latest_day()
            print(f"--- data ---\n  {n} daily rows, most recent: {latest}")
            if latest:
                row = adapter.daily(latest)
                if row:
                    shown = {k: v for k, v in row.items() if not k.startswith("_") and v is not None}
                    print(f"  latest row: {shown}")
                    if row.get("_suspect"):
                        print(f"  SUSPECT VALUES: {row['_suspect']}")
        except NoopDBError as exc:
            print(f"  read failed: {exc}", file=sys.stderr)
        print()

    if dump_sql:
        print("--- CREATE statements ---")
        with adapter._connect() as conn:  # noqa: SLF001 - diagnostics tool
            for row in conn.execute(
                "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ):
                print(f"  {row['sql']};\n")

    if smap.daily is None:
        print("Daily metrics UNRESOLVED. Copy the real table/column names into "
              "schema_map.json — see README 'When the schema does not resolve'.",
              file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Probe NOOP's SQLite schema (read-only).")
    parser.add_argument("--find", action="store_true", help="search for the NOOP database")
    parser.add_argument("--sql", action="store_true", help="dump CREATE TABLE statements")
    args = parser.parse_args(argv)
    return cmd_find() if args.find else cmd_probe(args.sql)


if __name__ == "__main__":
    sys.exit(main())
