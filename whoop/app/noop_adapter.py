"""Read-only adapter over NOOP's SQLite database.

THIS IS THE ONLY FILE THAT KNOWS WHAT NOOP'S SCHEMA LOOKS LIKE.

Everything above it speaks in the canonical vocabulary defined here. If a NOOP
update renames a table or column, this file is where it breaks and where it gets
fixed — nothing else needs to change.

Because NOOP's persistence layer (`WhoopStore`) is not published (see
SCHEMA_NOTES.md 0), the schema is *discovered at runtime* rather than hardcoded:
we read `sqlite_master`, score each table against the logical model recovered
from `StrandAnalytics`, and resolve canonical field names against candidate
column names. `schema_map.json` can pin anything the resolver gets wrong.

Safety rules, all enforced here:
  * `file:...?mode=ro` URI — the connection physically cannot write.
  * `PRAGMA query_only = ON` as a second barrier.
  * Fresh short-lived connection per call — never hold a handle on a file
    another process owns.
  * No `PRAGMA journal_mode` — that would be a write.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

# --------------------------------------------------------------------------
# Canonical model — the vocabulary the rest of the app speaks.
#
# Keys are canonical names; values are candidate column names. Matching is done
# on a normalised form (lowercase, underscores stripped), so `avg_hrv`, `avgHrv`
# and `AvgHRV` all collapse to `avghrv` and only genuinely different *words*
# need listing.
# --------------------------------------------------------------------------

DAILY_FIELDS: dict[str, tuple[str, ...]] = {
    "day": ("day", "date", "day_key", "calendar_day", "local_day"),
    "total_sleep_min": ("total_sleep_min", "sleep_min", "total_sleep_minutes", "tst_min"),
    "efficiency": ("efficiency", "sleep_efficiency"),
    "deep_min": ("deep_min", "deep_sleep_min", "sws_min"),
    "rem_min": ("rem_min", "rem_sleep_min"),
    "light_min": ("light_min", "light_sleep_min"),
    "disturbances": ("disturbances", "disturbance_count", "awakenings"),
    "resting_hr": ("resting_hr", "rhr", "resting_heart_rate"),
    "avg_hrv": ("avg_hrv", "hrv", "hrv_rmssd", "rmssd", "avg_rmssd"),
    "recovery": ("recovery", "recovery_score", "recovery_pct"),
    "strain": ("strain", "day_strain", "strain_score"),
    "exercise_count": ("exercise_count", "workout_count", "exercises"),
    "spo2_pct": ("spo2_pct", "spo2", "blood_oxygen", "spo2_percent"),
    "skin_temp_dev_c": ("skin_temp_dev_c", "skin_temp_dev", "skin_temp_deviation_c", "skin_temp_delta_c"),
    "resp_rate_bpm": ("resp_rate_bpm", "resp_rate", "respiratory_rate", "respiration"),
    "steps": ("steps", "step_count"),
    "active_kcal_est": ("active_kcal_est", "active_kcal", "active_calories", "kcal_est"),
    "sleep_start": ("sleep_start", "sleep_start_ts", "bed_start"),
    "sleep_end": ("sleep_end", "sleep_end_ts", "bed_end"),
}
"""Canonical daily-metric fields. See SCHEMA_NOTES.md 1.1 for units."""

DAILY_REQUIRED = ("day",)
DAILY_SIGNALS = ("recovery", "strain", "avg_hrv", "resting_hr", "total_sleep_min")

SLEEP_FIELDS: dict[str, tuple[str, ...]] = {
    "start_ts": ("start_ts", "start", "start_time", "started_at"),
    "end_ts": ("end_ts", "end", "end_time", "ended_at"),
    "efficiency": ("efficiency", "sleep_efficiency"),
    "resting_hr": ("resting_hr", "rhr", "resting_heart_rate"),
    "avg_hrv": ("avg_hrv", "hrv", "rmssd"),
    "stages_json": ("stages_json", "stages", "hypnogram_json", "hypnogram"),
}
SLEEP_REQUIRED = ("start_ts", "end_ts")
SLEEP_SIGNALS = ("stages_json", "efficiency")

HR_FIELDS: dict[str, tuple[str, ...]] = {
    "ts": ("ts", "timestamp", "time", "recorded_at", "sample_ts"),
    "bpm": ("bpm", "hr", "heart_rate", "value", "heart_rate_bpm"),
}
HR_REQUIRED = ("ts", "bpm")
HR_SIGNALS = ("bpm",)

BATTERY_CANDIDATES = ("battery", "battery_pct", "battery_level", "battery_percent", "soc")

SKIN_TEMP_FIELDS: dict[str, tuple[str, ...]] = {
    "ts": ("ts", "timestamp", "time", "recorded_at"),
    "celsius": ("celsius", "temp_c", "skin_temp_c", "value", "c"),
}

# Plausibility gates lifted verbatim from NOOP's Baselines.metricCfg. A value
# outside these is rejected by NOOP's own baseline model, so if we read one it is
# suspect and we flag rather than display it as fact. (SCHEMA_NOTES.md 2)
SANITY_BOUNDS: dict[str, tuple[float, float]] = {
    "avg_hrv": (5.0, 250.0),
    "resting_hr": (30.0, 120.0),
    "resp_rate_bpm": (4.0, 40.0),
    "recovery": (0.0, 100.0),
    "strain": (0.0, 21.0),
    "efficiency": (0.0, 1.0),
    "spo2_pct": (50.0, 100.0),
}


def _norm(name: str) -> str:
    return name.replace("_", "").replace(" ", "").lower()


class NoopDBError(RuntimeError):
    """Raised for anything the operator needs to fix — path, permissions, schema."""


class NoopDBUnavailable(NoopDBError):
    """The database file is not configured, missing, or unreadable."""


# --------------------------------------------------------------------------
# Schema discovery
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class TableMatch:
    """One resolved logical table."""

    table: str
    columns: dict[str, str]          # canonical -> actual column name
    missing: tuple[str, ...]         # canonical fields with no column
    score: int

    def col(self, canonical: str) -> str | None:
        return self.columns.get(canonical)

    def has(self, canonical: str) -> bool:
        return canonical in self.columns


@dataclass
class SchemaMap:
    """The result of introspecting a real NOOP database."""

    daily: TableMatch | None = None
    sleep: TableMatch | None = None
    hr: TableMatch | None = None
    skin_temp: TableMatch | None = None
    battery: tuple[str, str] | None = None    # (table, column)
    all_tables: dict[str, list[str]] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    overrides_applied: list[str] = field(default_factory=list)

    @property
    def resolved(self) -> bool:
        return self.daily is not None

    def describe(self) -> dict[str, Any]:
        """Diagnostics payload — surfaced at /api/diagnostics and by `probe`."""

        def one(m: TableMatch | None) -> dict[str, Any] | None:
            if m is None:
                return None
            return {"table": m.table, "columns": m.columns,
                    "missing": list(m.missing), "score": m.score}

        return {
            "daily": one(self.daily),
            "sleep": one(self.sleep),
            "heart_rate": one(self.hr),
            "skin_temp": one(self.skin_temp),
            "battery": {"table": self.battery[0], "column": self.battery[1]} if self.battery else None,
            "tables_found": sorted(self.all_tables),
            "notes": self.notes,
            "overrides_applied": self.overrides_applied,
        }


def _match_table(
    table: str,
    columns: list[str],
    spec: dict[str, tuple[str, ...]],
    required: tuple[str, ...],
    signals: tuple[str, ...],
) -> TableMatch | None:
    """Score one physical table against one logical spec.

    A table qualifies only if every `required` field resolves. Score is the count
    of resolved fields, with `signals` weighted so that e.g. a table holding
    `recovery`/`strain` beats one that merely has a `day` column.
    """
    by_norm = {_norm(c): c for c in columns}
    resolved: dict[str, str] = {}
    for canonical, candidates in spec.items():
        for cand in candidates:
            actual = by_norm.get(_norm(cand))
            if actual is not None:
                resolved[canonical] = actual
                break

    if any(r not in resolved for r in required):
        return None
    if not any(s in resolved for s in signals):
        return None

    score = len(resolved) + 3 * sum(1 for s in signals if s in resolved)
    missing = tuple(k for k in spec if k not in resolved)
    return TableMatch(table=table, columns=resolved, missing=missing, score=score)


def _best_match(
    tables: dict[str, list[str]],
    spec: dict[str, tuple[str, ...]],
    required: tuple[str, ...],
    signals: tuple[str, ...],
    exclude: set[str] | None = None,
) -> TableMatch | None:
    best: TableMatch | None = None
    for table, columns in tables.items():
        if exclude and table in exclude:
            continue
        m = _match_table(table, columns, spec, required, signals)
        if m is not None and (best is None or m.score > best.score):
            best = m
    return best


class NoopAdapter:
    """Read-only access to NOOP's database.

    All returned values use the canonical names and the units documented in
    SCHEMA_NOTES.md. Nothing here computes a metric — NOOP already did that
    on-device; we read its cache.
    """

    def __init__(self, db_path: Path | None, schema_map_path: Path | None = None):
        self.db_path = db_path
        self.schema_map_path = schema_map_path
        self._schema: SchemaMap | None = None

    # -- connection ------------------------------------------------------

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        if self.db_path is None:
            raise NoopDBUnavailable(
                "NOOP_DB_PATH is not set. Copy .env.example to .env and point it at "
                "your NOOP SQLite file, then run `python -m app.probe --find` if you "
                "are not sure where it lives."
            )
        if not self.db_path.is_file():
            raise NoopDBUnavailable(
                f"No file at NOOP_DB_PATH ({self.db_path}). "
                "Run `python -m app.probe --find` to search the usual locations."
            )
        uri = f"file:{self.db_path.as_posix()}?mode=ro"
        try:
            conn = sqlite3.connect(uri, uri=True, timeout=5.0)
        except sqlite3.OperationalError as exc:
            raise NoopDBUnavailable(
                f"Could not open {self.db_path} read-only: {exc}. If NOOP is running in "
                "WAL mode, the -wal and -shm sidecar files must be readable too."
            ) from exc
        try:
            conn.row_factory = sqlite3.Row
            # Second barrier: even a bug in this file cannot now write.
            conn.execute("PRAGMA query_only = ON")
            conn.execute("PRAGMA busy_timeout = 4000")
            yield conn
        finally:
            conn.close()

    # -- schema ----------------------------------------------------------

    def _load_overrides(self) -> dict[str, Any]:
        if self.schema_map_path is None or not self.schema_map_path.is_file():
            return {}
        try:
            return json.loads(self.schema_map_path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise NoopDBError(f"schema_map.json is unreadable: {exc}") from exc

    def schema(self, refresh: bool = False) -> SchemaMap:
        """Introspect the database and resolve the logical model. Cached."""
        if self._schema is not None and not refresh:
            return self._schema

        with self._connect() as conn:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','view') "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
            tables: dict[str, list[str]] = {}
            for row in rows:
                name = row["name"]
                cols = conn.execute(f'PRAGMA table_info("{name}")').fetchall()
                tables[name] = [c["name"] for c in cols]

        smap = SchemaMap(all_tables=tables)
        if not tables:
            smap.notes.append("Database contains no tables — is this the right file?")
            self._schema = smap
            return smap

        smap.daily = _best_match(tables, DAILY_FIELDS, DAILY_REQUIRED, DAILY_SIGNALS)
        used = {smap.daily.table} if smap.daily else set()
        smap.sleep = _best_match(tables, SLEEP_FIELDS, SLEEP_REQUIRED, SLEEP_SIGNALS, exclude=used)
        if smap.sleep:
            used.add(smap.sleep.table)
        smap.hr = _best_match(tables, HR_FIELDS, HR_REQUIRED, HR_SIGNALS, exclude=used)
        if smap.hr:
            used.add(smap.hr.table)
        smap.skin_temp = _best_match(
            tables, SKIN_TEMP_FIELDS, ("ts", "celsius"), ("celsius",), exclude=used
        )

        # Battery is not a DailyMetric field (SCHEMA_NOTES.md open question 4) —
        # look for it anywhere before declaring it unavailable.
        for table, cols in tables.items():
            by_norm = {_norm(c): c for c in cols}
            for cand in BATTERY_CANDIDATES:
                if _norm(cand) in by_norm:
                    smap.battery = (table, by_norm[_norm(cand)])
                    break
            if smap.battery:
                break

        self._apply_overrides(smap, tables)

        if smap.daily is None:
            smap.notes.append(
                "No table resolved as daily metrics. Run `python -m app.probe` to see "
                "the real schema, then pin it in schema_map.json."
            )
        else:
            if smap.daily.missing:
                smap.notes.append(
                    "Daily table resolved but these fields had no column: "
                    + ", ".join(smap.daily.missing)
                )
        if smap.hr is None:
            smap.notes.append(
                "No heart-rate sample table resolved — live/last HR and workout "
                "auto-detection will be unavailable."
            )
        if smap.battery is None:
            smap.notes.append(
                "No battery column found anywhere. NOOP may not persist strap battery "
                "(it is not a DailyMetric field); it may be live-BLE only."
            )
        migrations = [t for t in tables if "migration" in t.lower() or "version" in t.lower()]
        if migrations:
            smap.notes.append(f"Schema/migration tables present: {', '.join(migrations)}")

        self._schema = smap
        return smap

    def _apply_overrides(self, smap: SchemaMap, tables: dict[str, list[str]]) -> None:
        """Let schema_map.json pin a table and/or individual columns."""
        overrides = self._load_overrides()
        specs = {
            "daily": (DAILY_FIELDS, DAILY_REQUIRED, DAILY_SIGNALS),
            "sleep": (SLEEP_FIELDS, SLEEP_REQUIRED, SLEEP_SIGNALS),
            "hr": (HR_FIELDS, HR_REQUIRED, HR_SIGNALS),
        }
        for key, spec_tuple in specs.items():
            ov = overrides.get(key)
            if not isinstance(ov, dict):
                continue
            table = ov.get("table")
            if table and table not in tables:
                smap.notes.append(
                    f"schema_map.json pins {key}.table='{table}' but no such table exists."
                )
                continue
            current: TableMatch | None = getattr(smap, key)
            base_table = table or (current.table if current else None)
            if base_table is None:
                continue
            spec = spec_tuple[0]
            columns = dict(current.columns) if current and current.table == base_table else {}
            if table and (not current or current.table != table):
                auto = _match_table(base_table, tables[base_table], spec, (), ())
                columns = dict(auto.columns) if auto else {}
            for canonical, actual in (ov.get("columns") or {}).items():
                if canonical not in spec:
                    smap.notes.append(f"schema_map.json: unknown {key} field '{canonical}'")
                    continue
                if actual not in tables[base_table]:
                    smap.notes.append(
                        f"schema_map.json: {key}.{canonical} -> '{actual}' "
                        f"not a column of {base_table}"
                    )
                    continue
                columns[canonical] = actual
            missing = tuple(k for k in spec if k not in columns)
            setattr(smap, key, TableMatch(base_table, columns, missing, score=999))
            smap.overrides_applied.append(key)

    # -- reads -----------------------------------------------------------

    def _require_daily(self) -> TableMatch:
        smap = self.schema()
        if smap.daily is None:
            raise NoopDBError(
                "Could not identify NOOP's daily-metrics table in "
                f"{self.db_path}. Tables present: {', '.join(sorted(smap.all_tables)) or 'none'}. "
                "Run `python -m app.probe` and pin it in schema_map.json."
            )
        return smap.daily

    def _row_to_metrics(self, row: sqlite3.Row, match: TableMatch) -> dict[str, Any]:
        out: dict[str, Any] = {}
        suspect: list[str] = []
        for canonical, actual in match.columns.items():
            value = row[actual]
            if value is not None and canonical in SANITY_BOUNDS:
                lo, hi = SANITY_BOUNDS[canonical]
                if not (lo <= float(value) <= hi):
                    suspect.append(f"{canonical}={value} outside NOOP's own plausible range [{lo}, {hi}]")
            out[canonical] = value
        out["_suspect"] = suspect
        out["_unavailable"] = list(match.missing)
        return out

    def daily(self, day: str) -> dict[str, Any] | None:
        """One day's cached metrics, or None. `day` is a 'YYYY-MM-DD' key."""
        match = self._require_daily()
        day_col = match.col("day")
        cols = ", ".join(f'"{c}"' for c in match.columns.values())
        with self._connect() as conn:
            row = conn.execute(
                f'SELECT {cols} FROM "{match.table}" WHERE "{day_col}" = ? LIMIT 1', (day,)
            ).fetchone()
        return self._row_to_metrics(row, match) if row else None

    def daily_range(self, start_day: str, end_day: str) -> list[dict[str, Any]]:
        """Inclusive range of daily rows, ascending. Used by Phase 2 trends."""
        match = self._require_daily()
        day_col = match.col("day")
        cols = ", ".join(f'"{c}"' for c in match.columns.values())
        with self._connect() as conn:
            rows = conn.execute(
                f'SELECT {cols} FROM "{match.table}" '
                f'WHERE "{day_col}" >= ? AND "{day_col}" <= ? ORDER BY "{day_col}" ASC',
                (start_day, end_day),
            ).fetchall()
        return [self._row_to_metrics(r, match) for r in rows]

    def latest_day(self) -> str | None:
        """The most recent day key present, or None for an empty table."""
        match = self._require_daily()
        day_col = match.col("day")
        with self._connect() as conn:
            row = conn.execute(
                f'SELECT MAX("{day_col}") AS d FROM "{match.table}"'
            ).fetchone()
        return row["d"] if row and row["d"] else None

    def day_count(self) -> int:
        match = self._require_daily()
        with self._connect() as conn:
            return int(conn.execute(f'SELECT COUNT(*) AS n FROM "{match.table}"').fetchone()["n"])

    def sleep_sessions(self, start_ts: int, end_ts: int) -> list[dict[str, Any]]:
        """Sleep sessions overlapping [start_ts, end_ts], with stages decoded."""
        smap = self.schema()
        if smap.sleep is None:
            return []
        match = smap.sleep
        cols = ", ".join(f'"{c}"' for c in match.columns.values())
        s_col, e_col = match.col("start_ts"), match.col("end_ts")
        with self._connect() as conn:
            rows = conn.execute(
                f'SELECT {cols} FROM "{match.table}" '
                f'WHERE "{e_col}" >= ? AND "{s_col}" <= ? ORDER BY "{s_col}" ASC',
                (start_ts, end_ts),
            ).fetchall()

        out: list[dict[str, Any]] = []
        for row in rows:
            rec = {c: row[a] for c, a in match.columns.items()}
            raw_stages = rec.pop("stages_json", None)
            rec["stages"] = _decode_stages(raw_stages)
            out.append(rec)
        return out

    def last_heart_rate(self, within_seconds: int = 6 * 3600) -> dict[str, Any] | None:
        """Most recent HR sample, if one exists inside the window."""
        smap = self.schema()
        if smap.hr is None:
            return None
        match = smap.hr
        ts_col, bpm_col = match.col("ts"), match.col("bpm")
        cutoff = int(datetime.now(tz=timezone.utc).timestamp()) - within_seconds
        with self._connect() as conn:
            row = conn.execute(
                f'SELECT "{ts_col}" AS ts, "{bpm_col}" AS bpm FROM "{match.table}" '
                f'WHERE "{ts_col}" >= ? ORDER BY "{ts_col}" DESC LIMIT 1',
                (cutoff,),
            ).fetchone()
        if row is None:
            return None
        return {"ts": int(row["ts"]), "bpm": int(row["bpm"])}

    def heart_rate_range(self, start_ts: int, end_ts: int, max_points: int = 2000) -> list[dict[str, int]]:
        """HR samples in a window, decimated to at most `max_points`."""
        smap = self.schema()
        if smap.hr is None:
            return []
        match = smap.hr
        ts_col, bpm_col = match.col("ts"), match.col("bpm")
        with self._connect() as conn:
            total = conn.execute(
                f'SELECT COUNT(*) AS n FROM "{match.table}" WHERE "{ts_col}" BETWEEN ? AND ?',
                (start_ts, end_ts),
            ).fetchone()["n"]
            # Decimate by row position, not by timestamp arithmetic: samples sit on
            # a regular grid (~60 s), so `ts % stride` aliases against that grid and
            # returns far more rows than asked for.
            stride = max(1, -(-int(total) // max_points)) if total else 1
            rows = conn.execute(
                f'SELECT ts, bpm FROM ('
                f'  SELECT "{ts_col}" AS ts, "{bpm_col}" AS bpm, '
                f'         ROW_NUMBER() OVER (ORDER BY "{ts_col}") AS rn '
                f'  FROM "{match.table}" WHERE "{ts_col}" BETWEEN ? AND ?'
                f') WHERE ((rn - 1) % ?) = 0 ORDER BY ts ASC',
                (start_ts, end_ts, stride),
            ).fetchall()
        return [{"ts": int(r["ts"]), "bpm": int(r["bpm"])} for r in rows]

    def battery(self) -> dict[str, Any] | None:
        """Strap battery, if NOOP persists it at all. Usually None — see notes."""
        smap = self.schema()
        if smap.battery is None:
            return None
        table, column = smap.battery
        with self._connect() as conn:
            cols = [c["name"] for c in conn.execute(f'PRAGMA table_info("{table}")').fetchall()]
            order = next(
                (c for c in cols if _norm(c) in {"ts", "timestamp", "time", "recordedat", "id"}),
                None,
            )
            sql = f'SELECT "{column}" AS pct FROM "{table}"'
            if order:
                sql += f' WHERE "{column}" IS NOT NULL ORDER BY "{order}" DESC'
            sql += " LIMIT 1"
            row = conn.execute(sql).fetchone()
        if row is None or row["pct"] is None:
            return None
        return {"percent": float(row["pct"]), "source": f"{table}.{column}"}


def _decode_stages(raw: Any) -> list[dict[str, Any]]:
    """Decode CachedSleepSession.stages_json (SCHEMA_NOTES.md 1.2)."""
    if not raw:
        return []
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", errors="replace")
    if not isinstance(raw, str):
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    out = []
    for seg in parsed:
        if isinstance(seg, dict) and {"start", "end", "stage"} <= set(seg):
            out.append({"start": int(seg["start"]), "end": int(seg["end"]),
                        "stage": str(seg["stage"])})
    return out


def stage_totals(stages: list[dict[str, Any]]) -> dict[str, float]:
    """Minutes per stage. Stage strings are NOOP's: wake|light|deep|rem."""
    totals: dict[str, float] = {}
    for seg in stages:
        dur = max(0, int(seg["end"]) - int(seg["start"])) / 60.0
        totals[seg["stage"]] = totals.get(seg["stage"], 0.0) + dur
    return totals


def utc_day_key(when: datetime) -> str:
    """NOOP buckets by UTC calendar day (SCHEMA_NOTES.md 1.4)."""
    return when.astimezone(timezone.utc).strftime("%Y-%m-%d")


def day_bounds_utc(day: str) -> tuple[int, int]:
    """[start, end) unix seconds for a UTC day key."""
    d = date.fromisoformat(day)
    start = datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
    return int(start.timestamp()), int((start + timedelta(days=1)).timestamp())
