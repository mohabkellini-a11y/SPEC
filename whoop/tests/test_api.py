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


def test_strap_state_is_honest_about_phase_1(client: TestClient):
    body = client.get("/api/strap/state").json()
    assert body["implemented"] is False
    assert body["state"] == "idle"
    assert "Phase 4" in body["message"]


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
