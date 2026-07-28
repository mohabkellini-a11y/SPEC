"""Adapter tests — schema resolution, read-only enforcement, decoding.

These run against the synthetic fixture (tools/make_fixture.py), which is a
reconstruction, not NOOP's real schema. What they actually prove is that the
*resolver* behaves correctly against a plausible shape and its variants — which
is the part that has to survive meeting a real database.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.noop_adapter import (  # noqa: E402
    NoopAdapter,
    NoopDBError,
    NoopDBUnavailable,
    _decode_stages,
    _norm,
    day_bounds_utc,
    stage_totals,
    utc_day_key,
)
from tools.make_fixture import build  # noqa: E402


@pytest.fixture(scope="module")
def snake_db(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("noop") / "snake.sqlite3"
    build(path, days=40, camel=False, cold_start=False)
    return path


@pytest.fixture(scope="module")
def camel_db(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("noop") / "camel.sqlite3"
    build(path, days=20, camel=True, cold_start=False)
    return path


# --- schema resolution -----------------------------------------------------


def test_resolves_snake_case_schema(snake_db: Path):
    smap = NoopAdapter(snake_db).schema()
    assert smap.resolved
    assert smap.daily.table == "daily_metrics"
    assert smap.daily.col("avg_hrv") == "avg_hrv"
    assert smap.daily.col("recovery") == "recovery"
    assert smap.sleep.table == "sleep_sessions"
    assert smap.hr.table == "hr_samples"


def test_resolves_camel_case_schema(camel_db: Path):
    """The whole point of runtime resolution: naming style must not matter."""
    smap = NoopAdapter(camel_db).schema()
    assert smap.resolved
    assert smap.daily.col("avg_hrv") == "avgHrv"
    assert smap.daily.col("skin_temp_dev_c") == "skinTempDevC"
    assert smap.daily.col("total_sleep_min") == "totalSleepMin"
    assert smap.sleep.col("stages_json") == "stagesJson"


def test_norm_collapses_naming_styles():
    assert _norm("avg_hrv") == _norm("avgHrv") == _norm("AVG HRV") == "avghrv"


def test_migration_table_is_not_mistaken_for_data(snake_db: Path):
    smap = NoopAdapter(snake_db).schema()
    assert smap.daily.table != "grdb_migrations"
    assert any("grdb_migrations" in n for n in smap.notes)


def test_battery_reported_unavailable_when_absent(snake_db: Path):
    adapter = NoopAdapter(snake_db)
    assert adapter.schema().battery is None
    assert adapter.battery() is None
    assert any("battery" in n.lower() for n in adapter.schema().notes)


def test_unresolvable_database_does_not_crash(tmp_path: Path):
    path = tmp_path / "unrelated.sqlite3"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE recipes (name TEXT, calories INT)")
    conn.commit()
    conn.close()

    smap = NoopAdapter(path).schema()
    assert not smap.resolved
    assert "recipes" in smap.all_tables
    with pytest.raises(NoopDBError, match="daily-metrics table"):
        NoopAdapter(path).daily("2026-07-28")


def test_missing_and_unconfigured_paths_give_actionable_errors(tmp_path: Path):
    with pytest.raises(NoopDBUnavailable, match="NOOP_DB_PATH is not set"):
        NoopAdapter(None).schema()
    with pytest.raises(NoopDBUnavailable, match="probe --find"):
        NoopAdapter(tmp_path / "nope.sqlite3").schema()


# --- overrides -------------------------------------------------------------


def test_schema_map_override_pins_a_column(tmp_path: Path, snake_db: Path):
    override = tmp_path / "schema_map.json"
    override.write_text(json.dumps({"daily": {"columns": {"recovery": "strain"}}}))
    smap = NoopAdapter(snake_db, override).schema()
    assert smap.daily.col("recovery") == "strain"
    assert "daily" in smap.overrides_applied


def test_bad_override_is_reported_not_silently_ignored(tmp_path: Path, snake_db: Path):
    override = tmp_path / "schema_map.json"
    override.write_text(json.dumps({"daily": {"columns": {"recovery": "nonexistent_col"}}}))
    smap = NoopAdapter(snake_db, override).schema()
    assert any("nonexistent_col" in n for n in smap.notes)


# --- read-only enforcement -------------------------------------------------


def test_connection_cannot_write(snake_db: Path):
    adapter = NoopAdapter(snake_db)
    with adapter._connect() as conn:  # noqa: SLF001
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("DELETE FROM daily_metrics")
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("CREATE TABLE evil (x INT)")


def test_query_only_pragma_is_on(snake_db: Path):
    with NoopAdapter(snake_db)._connect() as conn:  # noqa: SLF001
        assert conn.execute("PRAGMA query_only").fetchone()[0] == 1


def test_reads_do_not_modify_the_file(snake_db: Path):
    before = snake_db.stat().st_mtime_ns, snake_db.stat().st_size
    adapter = NoopAdapter(snake_db)
    adapter.daily(adapter.latest_day())
    adapter.daily_range("2026-01-01", "2030-01-01")
    adapter.last_heart_rate()
    assert (snake_db.stat().st_mtime_ns, snake_db.stat().st_size) == before


# --- reads -----------------------------------------------------------------


def test_daily_returns_canonical_names(snake_db: Path):
    adapter = NoopAdapter(snake_db)
    row = adapter.daily(adapter.latest_day())
    assert row is not None
    for field in ("day", "recovery", "strain", "avg_hrv", "resting_hr", "total_sleep_min"):
        assert field in row


def test_daily_returns_none_for_absent_day(snake_db: Path):
    assert NoopAdapter(snake_db).daily("1999-01-01") is None


def test_daily_range_is_ascending_and_inclusive(snake_db: Path):
    adapter = NoopAdapter(snake_db)
    latest = adapter.latest_day()
    rows = adapter.daily_range("2000-01-01", latest)
    assert rows
    days = [r["day"] for r in rows]
    assert days == sorted(days)
    assert days[-1] == latest


def test_camel_db_reads_through_canonical_names(camel_db: Path):
    adapter = NoopAdapter(camel_db)
    row = adapter.daily(adapter.latest_day())
    assert row["avg_hrv"] is not None       # canonical name, camelCase column
    assert "avgHrv" not in row


def test_sleep_sessions_decode_stages(snake_db: Path):
    adapter = NoopAdapter(snake_db)
    latest = adapter.latest_day()
    start, end = day_bounds_utc(latest)
    sessions = adapter.sleep_sessions(start - 86400, end)
    assert sessions
    stages = sessions[-1]["stages"]
    assert stages and all({"start", "end", "stage"} <= set(s) for s in stages)
    assert set(s["stage"] for s in stages) <= {"wake", "light", "deep", "rem"}


def test_heart_rate_decimation_respects_max_points(snake_db: Path):
    adapter = NoopAdapter(snake_db)
    latest = adapter.latest_day()
    start, end = day_bounds_utc(latest)
    samples = adapter.heart_rate_range(start, end, max_points=50)
    assert len(samples) <= 50           # ceil-stride guarantees the cap
    assert samples == sorted(samples, key=lambda s: s["ts"])


def test_suspect_values_are_flagged(tmp_path: Path):
    """A resting HR of 5 bpm is outside NOOP's own baseline gate — flag, don't show."""
    path = tmp_path / "bad.sqlite3"
    build(path, days=5, camel=False, cold_start=False)
    conn = sqlite3.connect(path)
    conn.execute("UPDATE daily_metrics SET resting_hr = 5 WHERE day = (SELECT MAX(day) FROM daily_metrics)")
    conn.commit()
    conn.close()

    adapter = NoopAdapter(path)
    row = adapter.daily(adapter.latest_day())
    assert row["_suspect"]
    assert "resting_hr" in row["_suspect"][0]


def test_cold_start_leaves_recovery_null(tmp_path: Path):
    """NOOP's RecoveryScorer returns nil for <4 nights. Null must survive as null."""
    path = tmp_path / "cold.sqlite3"
    build(path, days=10, camel=False, cold_start=True)
    adapter = NoopAdapter(path)
    row = adapter.daily(adapter.latest_day())
    assert row["recovery"] is None
    assert row["avg_hrv"] is not None       # other metrics still present


# --- pure helpers ----------------------------------------------------------


def test_stage_totals_sums_minutes():
    stages = [
        {"start": 0, "end": 600, "stage": "light"},      # 10 min
        {"start": 600, "end": 1800, "stage": "deep"},    # 20 min
        {"start": 1800, "end": 2400, "stage": "light"},  # 10 min
    ]
    assert stage_totals(stages) == {"light": 20.0, "deep": 20.0}


def test_decode_stages_tolerates_garbage():
    assert _decode_stages(None) == []
    assert _decode_stages("") == []
    assert _decode_stages("not json") == []
    assert _decode_stages('{"not": "a list"}') == []
    assert _decode_stages('[{"start": 1, "end": 2}]') == []          # missing 'stage'
    assert _decode_stages('[{"start": 1, "end": 2, "stage": "rem"}]') == [
        {"start": 1, "end": 2, "stage": "rem"}
    ]


def test_decode_stages_accepts_bytes():
    raw = json.dumps([{"start": 1, "end": 2, "stage": "deep"}]).encode()
    assert _decode_stages(raw)[0]["stage"] == "deep"


def test_day_bounds_are_utc_midnight_to_midnight():
    start, end = day_bounds_utc("2026-07-28")
    assert end - start == 86400
    assert utc_day_key(__import__("datetime").datetime.fromtimestamp(
        start, tz=__import__("datetime").timezone.utc)) == "2026-07-28"
