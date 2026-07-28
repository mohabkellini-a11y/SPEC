"""API tests for the journal, habits and workout log (Phase 3).

The recurring concern: this app's database and NOOP's must stay independent.
The journal has to work with NOOP missing, and NOOP's file must never be
touched by anything here.
"""

from __future__ import annotations

import importlib
import sqlite3
import sys
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
    import app.config
    import app.routes_journal
    import app.main
    importlib.reload(app.config)
    importlib.reload(app.routes_journal)
    importlib.reload(app.main)
    return TestClient(app.main.app)


@pytest.fixture
def noop_db(tmp_path: Path) -> Path:
    path = tmp_path / "noop.sqlite3"
    build(path, days=30, camel=False, cold_start=False)
    return path


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, noop_db: Path) -> TestClient:
    return make_client(tmp_path, monkeypatch, noop_db)


def today_of(client: TestClient) -> str:
    return client.get("/api/today").json()["requested_day"]


# --- habits ----------------------------------------------------------------


def test_starter_habits_are_seeded(client: TestClient):
    body = client.get("/api/habits").json()
    assert len(body["habits"]) == 5
    assert {"bool", "scale"} <= {h["type"] for h in body["habits"]}
    assert set(body["types"]) == {"bool", "scale", "number"}


def test_create_and_update_a_habit(client: TestClient):
    created = client.post("/api/habits", json={
        "key": "magnesium", "label": "Magnesium", "type": "number", "unit": "mg",
    })
    assert created.status_code == 201
    habit = created.json()
    assert habit["unit"] == "mg"

    renamed = client.patch(f"/api/habits/{habit['id']}", json={"label": "Mg"})
    assert renamed.json()["label"] == "Mg"


def test_create_habit_rejects_bad_type(client: TestClient):
    res = client.post("/api/habits", json={"key": "x", "label": "X", "type": "vibes"})
    assert res.status_code == 400


def test_duplicate_habit_key_is_rejected(client: TestClient):
    payload = {"key": "caffeine", "label": "Caffeine", "type": "bool"}
    assert client.post("/api/habits", json=payload).status_code == 400


def test_delete_archives_by_default_and_keeps_history(client: TestClient):
    day = today_of(client)
    habit = client.get("/api/habits").json()["habits"][0]
    client.put(f"/api/journal/{day}", json={"values": {habit["id"]: 1}})

    res = client.delete(f"/api/habits/{habit['id']}")
    assert res.json() == {"deleted": False, "archived": True,
                          "habit": res.json()["habit"], "note": res.json()["note"]}
    assert habit["id"] not in {h["id"] for h in client.get("/api/habits").json()["habits"]}
    # The logged value survives.
    entries = client.get("/api/journal?days=7").json()["entries"]
    assert any(v for v in entries.values() if v["values"])


def test_purge_deletes_habit_and_history(client: TestClient):
    day = today_of(client)
    habit = client.get("/api/habits").json()["habits"][0]
    client.put(f"/api/journal/{day}", json={"values": {habit["id"]: 1}})
    assert client.delete(f"/api/habits/{habit['id']}?purge=true").json()["deleted"] is True
    entries = client.get("/api/journal?days=7").json()["entries"]
    assert not any(v["values"] for v in entries.values())


# --- journal ---------------------------------------------------------------


def test_journal_round_trip(client: TestClient):
    day = today_of(client)
    habits = client.get("/api/habits").json()["habits"]
    res = client.put(f"/api/journal/{day}", json={
        "values": {habits[0]["id"]: 1, habits[3]["id"]: 4},
        "note": "late espresso",
    })
    body = res.json()
    assert body["entry_count"] == 2
    assert body["note"] == "late espresso"

    reloaded = client.get(f"/api/journal/{day}").json()
    assert reloaded["entry_count"] == 2
    assert reloaded["is_today"] is True


def test_journal_partial_update_leaves_other_values_alone(client: TestClient):
    day = today_of(client)
    habits = client.get("/api/habits").json()["habits"]
    client.put(f"/api/journal/{day}", json={"values": {habits[0]["id"]: 1}})
    client.put(f"/api/journal/{day}", json={"note": "just a note"})
    body = client.get(f"/api/journal/{day}").json()
    assert body["entry_count"] == 1
    assert body["note"] == "just a note"


def test_clearing_a_value_differs_from_setting_zero(client: TestClient):
    day = today_of(client)
    habit = next(h for h in client.get("/api/habits").json()["habits"] if h["type"] == "bool")

    client.put(f"/api/journal/{day}", json={"values": {habit["id"]: 0}})
    body = client.get(f"/api/journal/{day}").json()
    assert next(h for h in body["habits"] if h["id"] == habit["id"])["value"] == 0
    assert body["entry_count"] == 1

    client.put(f"/api/journal/{day}", json={"values": {habit["id"]: None}})
    body = client.get(f"/api/journal/{day}").json()
    assert next(h for h in body["habits"] if h["id"] == habit["id"])["value"] is None
    assert body["entry_count"] == 0


def test_invalid_value_is_rejected_with_a_readable_message(client: TestClient):
    day = today_of(client)
    habit = next(h for h in client.get("/api/habits").json()["habits"] if h["type"] == "scale")
    res = client.put(f"/api/journal/{day}", json={"values": {habit["id"]: 42}})
    assert res.status_code == 400
    assert "between 1 and 5" in res.json()["detail"]


def test_backfilling_a_past_day(client: TestClient):
    habit = client.get("/api/habits").json()["habits"][0]
    res = client.put("/api/journal/2026-01-15", json={"values": {habit["id"]: 1}})
    assert res.status_code == 200
    assert res.json()["entry_count"] == 1
    assert client.get("/api/journal/2026-01-15").json()["is_today"] is False


def test_malformed_day_is_rejected(client: TestClient):
    assert client.get("/api/journal/2026-13-45").status_code == 422
    assert client.get("/api/journal/yesterday").status_code == 422


def test_journal_range_lists_logged_days(client: TestClient):
    habit = client.get("/api/habits").json()["habits"][0]
    day = today_of(client)
    client.put(f"/api/journal/{day}", json={"values": {habit["id"]: 1}})
    body = client.get("/api/journal?days=30").json()
    assert day in body["logged_days"]
    assert body["entries"][day]["values"]


# --- workouts --------------------------------------------------------------


def test_log_list_edit_and_delete_a_workout(client: TestClient):
    day = today_of(client)
    created = client.post("/api/workouts", json={
        "day": day, "type": "Run", "duration_s": 2400, "exertion": 7, "notes": "hilly",
    })
    assert created.status_code == 201
    workout = created.json()
    assert workout["source"] == "manual"

    assert len(client.get("/api/workouts?days=7").json()["workouts"]) == 1

    edited = client.patch(f"/api/workouts/{workout['id']}", json={"exertion": 5})
    assert edited.json()["exertion"] == 5

    assert client.delete(f"/api/workouts/{workout['id']}").json()["deleted"] is True
    assert client.get("/api/workouts?days=7").json()["workouts"] == []


def test_workout_validation(client: TestClient):
    day = today_of(client)
    assert client.post("/api/workouts", json={
        "day": day, "type": "Run", "duration_s": 0}).status_code == 422
    assert client.post("/api/workouts", json={
        "day": day, "type": "Run", "duration_s": 600, "exertion": 11}).status_code == 422
    assert client.post("/api/workouts", json={
        "day": "not-a-day", "type": "Run", "duration_s": 600}).status_code == 422


def test_deleting_an_unknown_workout_is_a_400(client: TestClient):
    assert client.delete("/api/workouts/9999").status_code == 400


# --- suggestions -----------------------------------------------------------


def test_suggestions_are_found_in_the_fixture(client: TestClient):
    body = client.get("/api/workouts/suggestions?days=3").json()
    assert body["available"] is True
    assert body["suggestions"]
    first = body["suggestions"][0]
    assert first["duration_min"] >= 5
    assert first["peak_hr"] > first["avg_hr"] - 1
    assert "motion channel" in body["method_note"]


def test_confirming_a_suggestion_logs_it_and_hides_it(client: TestClient):
    body = client.get("/api/workouts/suggestions?days=3").json()
    s = body["suggestions"][0]

    res = client.post(
        f"/api/workouts/suggestions/{s['key']}/confirm?day={s['day']}",
        json={"type": "Ride", "exertion": 6},
    )
    assert res.status_code == 201
    workout = res.json()
    assert workout["source"] == "confirmed"
    assert workout["suggestion_key"] == s["key"]
    # HR detail carries across from the detection.
    assert workout["avg_hr"] == pytest.approx(s["avg_hr"], abs=0.1)
    assert workout["peak_hr"] == s["peak_hr"]
    assert workout["duration_s"] == s["duration_s"]

    after = client.get("/api/workouts/suggestions?days=3").json()
    assert s["key"] not in {x["key"] for x in after["suggestions"]}


def test_confirming_an_unknown_suggestion_is_a_404(client: TestClient):
    day = today_of(client)
    res = client.post(f"/api/workouts/suggestions/w1/confirm?day={day}", json={"type": "Run"})
    assert res.status_code == 404


def test_dismissal_persists_and_can_be_undone(client: TestClient):
    body = client.get("/api/workouts/suggestions?days=3").json()
    s = body["suggestions"][0]

    client.post(f"/api/workouts/suggestions/{s['key']}/dismiss?day={s['day']}")
    after = client.get("/api/workouts/suggestions?days=3").json()
    assert s["key"] not in {x["key"] for x in after["suggestions"]}
    assert after["n_hidden"] >= 1

    client.delete(f"/api/workouts/suggestions/{s['key']}/dismiss")
    restored = client.get("/api/workouts/suggestions?days=3").json()
    assert s["key"] in {x["key"] for x in restored["suggestions"]}


def test_suggestions_unavailable_without_a_heart_rate_table(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db = tmp_path / "nohr.sqlite3"
    build(db, days=10, camel=False, cold_start=False)
    conn = sqlite3.connect(db)
    conn.execute("DROP TABLE hr_samples")
    conn.commit()
    conn.close()

    body = make_client(tmp_path, monkeypatch, db).get("/api/workouts/suggestions").json()
    assert body["available"] is False
    assert "heart-rate" in body["reason"]
    assert body["suggestions"] == []


def test_suggestions_unavailable_without_noop_but_do_not_500(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    body = make_client(tmp_path, monkeypatch, None).get("/api/workouts/suggestions").json()
    assert body["available"] is False
    assert body["suggestions"] == []


# --- summary ---------------------------------------------------------------


def test_summary_reports_volume_and_scatter(client: TestClient):
    day = today_of(client)
    client.post("/api/workouts", json={"day": day, "type": "Run", "duration_s": 1800})
    body = client.get("/api/workouts/summary?days=30").json()
    assert body["n_workouts"] == 1
    assert body["total_minutes"] == 30.0
    assert body["weekly_volume"]
    assert body["strain_recovery"]["available"] is True
    assert body["strain_recovery"]["points"]
    assert "omitted rather than plotted at zero" in body["strain_recovery"]["note"]


def test_summary_weekly_volume_covers_empty_weeks(client: TestClient):
    body = client.get("/api/workouts/summary?days=90").json()
    weeks = body["weekly_volume"]
    assert len(weeks) >= 12
    assert all(w["count"] == 0 for w in weeks)


def test_summary_scatter_unavailable_without_noop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    client = make_client(tmp_path, monkeypatch, None)
    body = client.get("/api/workouts/summary?days=30").json()
    assert body["strain_recovery"]["available"] is False
    assert body["strain_recovery"]["reason"]


# --- isolation -------------------------------------------------------------


def test_journal_works_with_noop_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The whole point of a separate database."""
    client = make_client(tmp_path, monkeypatch, None)
    habits = client.get("/api/habits").json()["habits"]
    assert habits

    res = client.put("/api/journal/2026-07-01", json={
        "values": {habits[0]["id"]: 1}, "note": "no strap data at all"})
    assert res.status_code == 200
    assert res.json()["entry_count"] == 1

    # ...while the NOOP-backed view correctly reports itself unavailable.
    assert client.get("/api/today").status_code == 503


def test_writing_the_journal_never_touches_noops_file(client: TestClient, noop_db: Path):
    before = noop_db.stat().st_mtime_ns, noop_db.stat().st_size
    day = today_of(client)
    habits = client.get("/api/habits").json()["habits"]
    client.put(f"/api/journal/{day}", json={"values": {habits[0]["id"]: 1}, "note": "hi"})
    client.post("/api/workouts", json={"day": day, "type": "Run", "duration_s": 600})
    body = client.get("/api/workouts/suggestions?days=3").json()
    if body["suggestions"]:
        s = body["suggestions"][0]
        client.post(f"/api/workouts/suggestions/{s['key']}/dismiss?day={s['day']}")

    assert (noop_db.stat().st_mtime_ns, noop_db.stat().st_size) == before


def test_health_reports_both_databases(client: TestClient):
    body = client.get("/api/health").json()
    assert body["schema_resolved"] is True
    assert body["app_db"]["ok"] is True
    assert body["app_db"]["habits"] == 5


def test_health_ok_when_only_the_app_db_exists(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    body = make_client(tmp_path, monkeypatch, None).get("/api/health").json()
    assert body["ok"] is True
    assert body["schema_resolved"] is False
    assert body["app_db"]["ok"] is True


def test_store_stats_endpoint(client: TestClient):
    from app.store import SCHEMA_VERSION
    body = client.get("/api/store/stats").json()
    assert body["schema_version"] == SCHEMA_VERSION
    assert set(body) >= {"habits", "habit_entries", "day_notes", "workouts", "alarms"}
