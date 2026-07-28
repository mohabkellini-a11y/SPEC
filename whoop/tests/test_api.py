"""API tests — the honest-empty-state contract, mostly.

The dashboard's job in Phase 1 is to never show a number it does not have and to
say *why* when it cannot. These tests pin that behaviour.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.make_fixture import build  # noqa: E402


def make_client(db_path: Path | None, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """Rebuild the app against a given database path."""
    monkeypatch.setenv("NOOP_DB_PATH", str(db_path) if db_path else "")
    monkeypatch.setenv("DISPLAY_TZ", "UTC")
    import app.config
    import app.main
    importlib.reload(app.config)
    importlib.reload(app.main)
    return TestClient(app.main.app)


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    db = tmp_path / "noop.sqlite3"
    build(db, days=30, camel=False, cold_start=False)
    return make_client(db, monkeypatch)


# --- health / diagnostics --------------------------------------------------


def test_health_reports_resolved_schema(client: TestClient):
    body = client.get("/api/health").json()
    assert body["ok"] is True
    assert body["noop_db_present"] is True
    assert body["schema_resolved"] is True
    assert body["day_count"] == 30


def test_diagnostics_exposes_resolution(client: TestClient):
    body = client.get("/api/diagnostics").json()
    assert body["schema"]["daily"]["table"] == "daily_metrics"
    assert body["schema"]["heart_rate"]["table"] == "hr_samples"
    assert "not published" in body["schema_note"]


def test_metrics_meta_labels_every_tile(client: TestClient):
    body = client.get("/api/metrics/meta").json()
    for key in body["today_tiles"]:
        meta = body["metrics"][key]
        assert meta["kind"] in {"measured", "derived", "approximate"}
        assert meta["method"], f"{key} has no documented method"
    assert "not a medical device" in body["disclaimer"].lower()


# --- today -----------------------------------------------------------------


def test_today_returns_current_day(client: TestClient):
    body = client.get("/api/today").json()
    assert body["has_data"] is True
    assert body["is_stale"] is False
    assert body["days_behind"] == 0
    assert body["metrics"]["recovery"] is not None
    assert body["disclaimer"]


def test_today_exposes_sleep_stage_minutes(client: TestClient):
    body = client.get("/api/today").json()
    stages = body["sleep"]["stage_minutes"]
    assert stages
    assert set(stages) <= {"wake", "light", "deep", "rem"}
    assert all(v > 0 for v in stages.values())


def test_today_bookkeeping_keys_are_not_leaked(client: TestClient):
    metrics = client.get("/api/today").json()["metrics"]
    assert not any(k.startswith("_") for k in metrics)


def test_absent_day_falls_back_and_says_so(client: TestClient):
    body = client.get("/api/today?day=2030-01-01").json()
    assert body["requested_day"] == "2030-01-01"
    assert body["is_stale"] is True
    assert body["days_behind"] > 0
    assert body["resolved_day"] == body["latest_day"]


def test_absent_day_without_fallback_reports_no_data(client: TestClient):
    body = client.get("/api/today?day=2030-01-01&fallback=false").json()
    assert body["has_data"] is False
    assert body["metrics"] == {}
    assert body["resolved_day"] is None


def test_malformed_day_is_rejected(client: TestClient):
    assert client.get("/api/today?day=tomorrow").status_code == 422


def test_battery_absent_is_explicit_not_zero(client: TestClient):
    body = client.get("/api/today").json()
    assert body["battery_available"] is False
    assert body["battery"] is None


def test_cold_start_reports_calibrating_not_zero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db = tmp_path / "cold.sqlite3"
    build(db, days=12, camel=False, cold_start=True)
    body = make_client(db, monkeypatch).get("/api/today").json()
    assert body["has_data"] is True
    assert body["metrics"]["recovery"] is None
    assert body["calibrating"] is True


# --- heart rate ------------------------------------------------------------


def test_heart_rate_returns_samples(client: TestClient):
    latest = client.get("/api/today").json()["latest_day"]
    body = client.get(f"/api/heart-rate?day={latest}").json()
    assert body["available"] is True
    assert len(body["samples"]) > 10
    ts = [s["ts"] for s in body["samples"]]
    assert ts == sorted(ts)


def test_heart_rate_respects_max_points(client: TestClient):
    latest = client.get("/api/today").json()["latest_day"]
    body = client.get(f"/api/heart-rate?day={latest}&max_points=60").json()
    assert len(body["samples"]) <= 60


# --- strap state -----------------------------------------------------------


def test_strap_state_reports_idle_and_the_never_hold_policy(client: TestClient):
    """No connection is held while idle — the brief's hard constraint."""
    body = client.get("/api/strap/state").json()
    assert body["state"] == "idle"
    assert "never held while idle" in body["connection_policy"].replace(
        "is ever held while idle", "never held while idle")
    assert body["available"] is False          # no STRAP_ADDRESS in tests
    assert "bench_strap" in body["hint"]


# --- failure modes ---------------------------------------------------------


def test_missing_database_returns_503_with_guidance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    client = make_client(tmp_path / "does-not-exist.sqlite3", monkeypatch)
    res = client.get("/api/today")
    assert res.status_code == 503
    assert "probe --find" in res.json()["detail"]


def test_unconfigured_database_returns_503(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    client = make_client(None, monkeypatch)
    res = client.get("/api/today")
    assert res.status_code == 503
    assert "NOOP_DB_PATH" in res.json()["detail"]


def test_unresolvable_schema_returns_503_with_table_list(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import sqlite3
    db = tmp_path / "wrong.sqlite3"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE photos (id INT, caption TEXT)")
    conn.commit()
    conn.close()

    res = make_client(db, monkeypatch).get("/api/today")
    assert res.status_code == 503
    detail = res.json()["detail"]
    assert detail["error"] == "schema_unresolved"
    assert "photos" in detail["tables_found"]
    assert "probe" in detail["next_step"]


def test_health_survives_a_missing_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Health must never 500 — it is what you check when things are broken."""
    body = make_client(tmp_path / "nope.sqlite3", monkeypatch).get("/api/health").json()
    assert body["ok"] is True
    assert body["schema_resolved"] is False
    assert body["noop_db_present"] is False


def test_browsing_before_the_record_starts_is_not_reported_as_stale(client: TestClient):
    """A day earlier than any stored row must not read as 'strap is behind'."""
    body = client.get("/api/today?day=2000-01-01").json()
    assert body["fell_back"] is True
    assert body["days_behind"] < 0
    assert body["is_stale"] is False


def test_explicit_day_without_fallback_reports_no_data_not_a_redirect(client: TestClient):
    body = client.get("/api/today?day=2000-01-01&fallback=false").json()
    assert body["has_data"] is False
    assert body["fell_back"] is False


# --- trends (Phase 2) ------------------------------------------------------


def test_trends_returns_all_default_metrics(client: TestClient):
    body = client.get("/api/trends?days=30").json()
    meta = client.get("/api/metrics/meta").json()
    assert set(body["series"]) == set(meta["trend_metrics"])
    assert body["rows_found"] == 30
    for series in body["series"].values():
        assert series["has_data"] is True
        assert len(series["days"]) == len(series["values"]) == len(series["rolling"]) == 30


def test_trends_window_lengths(client: TestClient):
    for days in (7, 30, 90):
        body = client.get(f"/api/trends?days={days}").json()
        assert len(body["series"]["recovery"]["days"]) == days


def test_trends_range_ends_on_the_requested_day(client: TestClient):
    body = client.get("/api/trends?days=7&end=2026-07-10").json()
    assert body["end_day"] == "2026-07-10"
    assert body["start_day"] == "2026-07-04"
    assert body["series"]["recovery"]["days"][-1] == "2026-07-10"


def test_trends_rejects_unknown_metric(client: TestClient):
    res = client.get("/api/trends?metrics=vibes")
    assert res.status_code == 400
    assert "vibes" in res.json()["detail"]


def test_trends_rejects_live_only_metrics(client: TestClient):
    """HR and battery have no per-day history; charting them would be a lie."""
    for metric in ("heart_rate", "battery"):
        res = client.get(f"/api/trends?metrics={metric}")
        assert res.status_code == 400
        assert "live reading" in res.json()["detail"]


def test_trends_subset_selection(client: TestClient):
    body = client.get("/api/trends?days=14&metrics=recovery,strain").json()
    assert set(body["series"]) == {"recovery", "strain"}


def test_trends_marks_unresolved_columns_unavailable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A field NOOP's DB does not have is reported, not charted as flat."""
    import sqlite3
    db = tmp_path / "partial.sqlite3"
    build(db, days=20, camel=False, cold_start=False)
    conn = sqlite3.connect(db)
    conn.executescript(
        "ALTER TABLE daily_metrics RENAME TO old;"
        "CREATE TABLE daily_metrics AS SELECT day, recovery, avg_hrv, strain FROM old;"
        "DROP TABLE old;"
    )
    conn.commit()
    conn.close()

    body = make_client(db, monkeypatch).get("/api/trends?days=10").json()
    assert body["series"]["resting_hr"]["unavailable"] is True
    assert "not a column" in body["series"]["resting_hr"]["reason"]
    assert body["series"]["recovery"]["unavailable"] is False


def test_trends_preserves_gaps(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import sqlite3
    db = tmp_path / "gappy.sqlite3"
    build(db, days=20, camel=False, cold_start=False)
    conn = sqlite3.connect(db)
    days = [r[0] for r in conn.execute("SELECT day FROM daily_metrics ORDER BY day")]
    conn.executemany("DELETE FROM daily_metrics WHERE day = ?", [(d,) for d in days[5:12]])
    conn.commit()
    conn.close()

    body = make_client(db, monkeypatch).get("/api/trends?days=20").json()
    # avg_hrv is populated every night, so every None here is a deleted row.
    series = body["series"]["avg_hrv"]
    assert series["summary"]["n_values"] == 13
    assert series["summary"]["coverage"] < 1.0
    # A gap must never be bridged by an interpolated value.
    assert len([v for v in series["values"] if v is None]) == 7

    # A NULL column counts as a gap too: recovery is nil for NOOP's first four
    # nights, so it has the seven deleted days plus its cold-start nulls.
    assert body["series"]["recovery"]["summary"]["n_values"] == 9


def test_trends_empty_window_is_not_an_error(client: TestClient):
    body = client.get("/api/trends?days=7&end=2000-01-07").json()
    series = body["series"]["recovery"]
    assert series["has_data"] is False
    assert series["summary"]["n_values"] == 0
    assert all(v is None for v in series["values"])


def test_trends_delta_not_comparable_when_sparse(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import sqlite3
    db = tmp_path / "thin.sqlite3"
    build(db, days=30, camel=False, cold_start=False)
    conn = sqlite3.connect(db)
    days = [r[0] for r in conn.execute("SELECT day FROM daily_metrics ORDER BY day")]
    conn.executemany("DELETE FROM daily_metrics WHERE day = ?", [(d,) for d in days[:-2]])
    conn.commit()
    conn.close()

    series = make_client(db, monkeypatch).get("/api/trends?days=30").json()["series"]["recovery"]
    assert series["delta"]["comparable"] is False


def test_trends_rolling_window_is_configurable(client: TestClient):
    body = client.get("/api/trends?days=30&rolling=14").json()
    assert body["rolling_window"] == 14
    assert body["series"]["recovery"]["rolling_window"] == 14


def test_trends_validates_bounds(client: TestClient):
    assert client.get("/api/trends?days=1").status_code == 422
    assert client.get("/api/trends?days=9999").status_code == 422
    assert client.get("/api/trends?rolling=1").status_code == 422


def test_trends_unresolved_schema_returns_503(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import sqlite3
    db = tmp_path / "nope.sqlite3"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE notes (id INT)")
    conn.commit()
    conn.close()
    assert make_client(db, monkeypatch).get("/api/trends").status_code == 503


def test_trends_carries_the_disclaimer_and_direction_hints(client: TestClient):
    body = client.get("/api/trends?days=7").json()
    assert "never interpolated" in body["note"]
    assert body["disclaimer"]
    assert "resting_hr" in body["direction"]["lower_is_better"]
    assert "strain" in body["direction"]["neutral"]
