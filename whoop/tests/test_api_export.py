"""Correlations and export API tests (Phase 5)."""

from __future__ import annotations

import csv
import importlib
import io
import json
import sys
import zipfile
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.make_fixture import build  # noqa: E402


def make_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                noop_db: Path | None = None) -> TestClient:
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "app.sqlite3"))
    monkeypatch.setenv("NOOP_DB_PATH", str(noop_db) if noop_db else "")
    monkeypatch.setenv("DISPLAY_TZ", "UTC")
    for name in ("app.config", "app.routes_journal", "app.routes_alarms",
                 "app.routes_export", "app.main"):
        importlib.reload(importlib.import_module(name))
    import app.main
    return TestClient(app.main.app)


@pytest.fixture
def noop_db(tmp_path: Path) -> Path:
    path = tmp_path / "noop.sqlite3"
    build(path, days=60, camel=False, cold_start=False)
    return path


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, noop_db: Path) -> TestClient:
    return make_client(tmp_path, monkeypatch, noop_db)


def seed_journal(client: TestClient, days: int = 30) -> dict[str, int]:
    habits = {h["key"]: h["id"] for h in client.get("/api/habits").json()["habits"]}
    today = date.fromisoformat(client.get("/api/today").json()["requested_day"])
    for back in range(days):
        day = (today - timedelta(days=back)).isoformat()
        client.put(f"/api/journal/{day}", json={"values": {
            habits["alcohol"]: 1 if back % 2 == 0 else 0,
            habits["screen_time"]: (back % 5) + 1,
        }})
    return habits


# --- correlations -----------------------------------------------------------


def test_options_lists_habits_and_correlatable_metrics(client: TestClient):
    body = client.get("/api/correlations/options").json()
    assert body["habits"]
    keys = {m["key"] for m in body["metrics"]}
    assert "recovery" in keys and "avg_hrv" in keys
    assert "heart_rate" not in keys and "battery" not in keys
    assert body["default_lag"] == 1
    assert "the day it ends" in body["lag_note"]


def test_correlation_returns_groups_and_caveats(client: TestClient):
    habits = seed_journal(client)
    body = client.get(
        f"/api/correlations?habit_id={habits['alcohol']}&metric=recovery&days=30").json()

    assert body["lag"] == 1
    assert len(body["groups"]) == 2
    assert body["n_pairs"] > 0
    assert body["caveats"]
    assert any("not what caused what" in c for c in body["caveats"])
    assert body["disclaimer"]
    assert len(body["timeline"]["days"]) == len(body["timeline"]["habit"]) == 30


def test_correlation_metric_window_extends_past_the_habit_window(client: TestClient):
    """With lag 1 the last habit day needs the NEXT day's metric to exist."""
    habits = seed_journal(client, days=10)
    body = client.get(
        f"/api/correlations?habit_id={habits['alcohol']}&metric=recovery&days=10&lag=1").json()
    last_habit_day = body["end_day"]
    paired = {p["habit_day"] for p in body["pairs"]}
    # The fixture ends today, so today's habit has no tomorrow-metric; every
    # earlier day should pair.
    assert len(paired) >= 8
    assert last_habit_day not in paired or body["n_pairs"] == 10


def test_correlation_rejects_unknown_and_live_metrics(client: TestClient):
    habits = seed_journal(client, days=5)
    assert client.get(
        f"/api/correlations?habit_id={habits['alcohol']}&metric=vibes").status_code == 400
    res = client.get(f"/api/correlations?habit_id={habits['alcohol']}&metric=heart_rate")
    assert res.status_code == 400
    assert "live reading" in res.json()["detail"]


def test_correlation_unknown_habit_is_404(client: TestClient):
    assert client.get("/api/correlations?habit_id=9999&metric=recovery").status_code == 404


def test_correlation_without_noop_is_503_not_a_fake_answer(tmp_path: Path,
                                                           monkeypatch: pytest.MonkeyPatch):
    client = make_client(tmp_path, monkeypatch, None)
    habits = {h["key"]: h["id"] for h in client.get("/api/habits").json()["habits"]}
    res = client.get(f"/api/correlations?habit_id={habits['alcohol']}&metric=recovery")
    assert res.status_code == 503


def test_correlation_with_no_journal_says_not_enough_data(client: TestClient):
    habits = {h["key"]: h["id"] for h in client.get("/api/habits").json()["habits"]}
    body = client.get(
        f"/api/correlations?habit_id={habits['alcohol']}&metric=recovery").json()
    assert body["n_pairs"] == 0
    assert body["enough_data"] is False
    assert "Not enough logged days" in body["summary"]


def test_lag_is_configurable_and_reported(client: TestClient):
    habits = seed_journal(client)
    body = client.get(
        f"/api/correlations?habit_id={habits['alcohol']}&metric=recovery&lag=0").json()
    assert body["lag"] == 0
    assert any("Usually you want lag 1" in c for c in body["caveats"])


# --- export -----------------------------------------------------------------


def test_export_summary_counts_everything(client: TestClient):
    seed_journal(client, days=10)
    body = client.get("/api/export/summary").json()
    assert body["counts"]["journal"] == 20          # 2 habits x 10 days
    assert body["counts"]["noop_daily_metrics"] > 0
    assert body["noop_available"] is True


def test_export_json_contains_every_table(client: TestClient):
    seed_journal(client, days=5)
    res = client.get("/api/export.json")
    assert res.status_code == 200
    assert "attachment" in res.headers["content-disposition"]

    body = json.loads(res.content)
    for table in ("habits", "journal", "day_notes", "workouts", "alarms",
                  "noop_daily_metrics", "noop_sleep_sessions"):
        assert table in body
    assert body["meta"]["disclaimer"]
    assert "approximation" in body["meta"]["note"]


def test_export_json_can_exclude_noop(client: TestClient):
    body = json.loads(client.get("/api/export.json?include_noop=false").content)
    assert "noop_daily_metrics" not in body
    assert body["meta"]["includes_noop_data"] is False


def test_export_zip_has_one_csv_per_table_plus_a_readme(client: TestClient):
    seed_journal(client, days=5)
    res = client.get("/api/export.csv")
    assert res.headers["content-type"] == "application/zip"

    archive = zipfile.ZipFile(io.BytesIO(res.content))
    names = set(archive.namelist())
    assert {"habits.csv", "journal.csv", "workouts.csv", "alarms.csv",
            "noop_daily_metrics.csv", "README.txt", "meta.json"} <= names

    readme = archive.read("README.txt").decode()
    assert "was NOT logged" in readme
    assert "approximation" in readme


def test_exported_journal_csv_resolves_habit_names(client: TestClient):
    seed_journal(client, days=3)
    archive = zipfile.ZipFile(io.BytesIO(client.get("/api/export.csv").content))
    rows = list(csv.DictReader(io.StringIO(archive.read("journal.csv").decode())))
    assert rows
    assert {"day", "habit_key", "habit_label", "value"} <= set(rows[0])
    assert any(r["habit_key"] == "alcohol" for r in rows)
    # A habit id alone would make the export useless outside this app.
    assert all(r["habit_label"] for r in rows)


def test_export_csv_of_an_empty_table_is_still_a_file(client: TestClient):
    archive = zipfile.ZipFile(io.BytesIO(client.get("/api/export.csv").content))
    assert archive.read("day_notes.csv") == b""


def test_single_table_export(client: TestClient):
    seed_journal(client, days=3)
    res = client.get("/api/export/journal.csv")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/csv")
    assert 'filename="journal.csv"' in res.headers["content-disposition"]
    assert b"habit_key" in res.content


def test_unknown_table_export_is_404(client: TestClient):
    res = client.get("/api/export/secrets.csv")
    assert res.status_code == 404
    assert "Available" in res.json()["detail"]


def test_export_works_without_noop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Your own data must be exportable even if NOOP is gone."""
    client = make_client(tmp_path, monkeypatch, None)
    body = json.loads(client.get("/api/export.json").content)
    assert body["habits"]
    assert body["noop_daily_metrics"] == []


# --- PWA assets -------------------------------------------------------------


def test_manifest_is_served_with_the_right_type(client: TestClient):
    res = client.get("/manifest.webmanifest")
    assert res.status_code == 200
    assert "manifest" in res.headers["content-type"]
    body = res.json()
    assert body["display"] == "standalone"
    assert body["start_url"] == "/"
    assert any(i["purpose"] == "maskable" for i in body["icons"])


def test_service_worker_is_served_from_the_root_scope(client: TestClient):
    """At /static/sw.js it could only control /static/, which is useless."""
    res = client.get("/sw.js")
    assert res.status_code == 200
    assert "javascript" in res.headers["content-type"]
    assert res.headers.get("Service-Worker-Allowed") == "/"


def test_icons_exist_and_are_real_pngs(client: TestClient):
    for name in ("icon-180.png", "icon-192.png", "icon-512.png",
                 "icon-192-maskable.png", "icon-512-maskable.png"):
        res = client.get(f"/static/icons/{name}")
        assert res.status_code == 200, name
        assert res.content[:8] == b"\x89PNG\r\n\x1a\n", name


def test_health_reports_phase_five(client: TestClient):
    assert client.get("/api/health").json()["phase"] == 5
