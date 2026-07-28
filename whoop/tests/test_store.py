"""App-store tests — migrations, validation, and the unset/zero distinction.

The store owns everything the user types. The theme here is that it must never
lose that data or quietly reinterpret it: clearing is not zeroing, archiving is
not deleting, and a bad value is rejected with a message rather than coerced.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.store import AppStore, StoreError  # noqa: E402


@pytest.fixture
def store(tmp_path: Path) -> AppStore:
    s = AppStore(tmp_path / "app.sqlite3")
    s.ensure_ready()
    return s


# --- migrations ------------------------------------------------------------


def test_migration_creates_schema_and_sets_version(tmp_path: Path):
    s = AppStore(tmp_path / "fresh.sqlite3")
    assert s.migrate() == 1
    assert s.version() == 1


def test_migration_is_idempotent(store: AppStore):
    before = store.stats()
    store.migrate()
    store.migrate()
    assert store.stats() == before


def test_database_file_is_created_with_parent_dirs(tmp_path: Path):
    s = AppStore(tmp_path / "nested" / "deeper" / "app.sqlite3")
    s.ensure_ready()
    assert s.path.is_file()


def test_seeds_starter_habits_once(store: AppStore):
    assert len(store.habits()) == 5
    assert store.seed_default_habits() == 0


def test_does_not_reseed_after_you_delete_them_all(store: AppStore):
    for habit in store.habits():
        store.delete_habit(habit.id)
    assert store.seed_default_habits() == 0
    assert store.habits() == []


# --- habit definitions -----------------------------------------------------


def test_create_habit_of_each_type(store: AppStore):
    for key, type_ in (("water", "number"), ("mood", "scale"), ("walk", "bool")):
        habit = store.create_habit(key=key, label=key.title(), type_=type_)
        assert habit.type == type_


def test_create_habit_rejects_unknown_type(store: AppStore):
    with pytest.raises(StoreError, match="Habit type"):
        store.create_habit(key="x", label="X", type_="vibes")


def test_create_habit_rejects_duplicate_key(store: AppStore):
    with pytest.raises(StoreError, match="already exists"):
        store.create_habit(key="caffeine", label="Caffeine again", type_="bool")


def test_create_habit_requires_key_and_label(store: AppStore):
    with pytest.raises(StoreError):
        store.create_habit(key="  ", label="X", type_="bool")
    with pytest.raises(StoreError):
        store.create_habit(key="x", label="   ", type_="bool")


def test_create_habit_rejects_inverted_bounds(store: AppStore):
    with pytest.raises(StoreError, match="below max_value"):
        store.create_habit(key="x", label="X", type_="number", min_value=10, max_value=2)


def test_archived_habits_are_hidden_but_kept(store: AppStore):
    habit = store.habits()[0]
    store.set_habit_value("2026-07-01", habit.id, 1)
    store.update_habit(habit.id, archived=True)

    assert habit.id not in {h.id for h in store.habits()}
    assert habit.id in {h.id for h in store.habits(include_archived=True)}
    # The history survives archiving.
    assert store.journal_range("2026-07-01", "2026-07-01")["2026-07-01"]["values"]


def test_deleting_a_habit_cascades_to_its_entries(store: AppStore):
    habit = store.habits()[0]
    store.set_habit_value("2026-07-01", habit.id, 1)
    store.delete_habit(habit.id)
    assert store.journal_range("2026-07-01", "2026-07-01") == {}


def test_unknown_habit_id_raises(store: AppStore):
    with pytest.raises(StoreError, match="No habit with id"):
        store.habit(9999)
    with pytest.raises(StoreError):
        store.update_habit(9999, label="x")
    with pytest.raises(StoreError):
        store.delete_habit(9999)


# --- value validation ------------------------------------------------------


def test_bool_habit_rejects_anything_but_zero_or_one(store: AppStore):
    habit = next(h for h in store.habits() if h.type == "bool")
    store.set_habit_value("2026-07-01", habit.id, 1)
    store.set_habit_value("2026-07-01", habit.id, 0)
    with pytest.raises(StoreError, match="yes/no"):
        store.set_habit_value("2026-07-01", habit.id, 5)


def test_scale_habit_enforces_range_and_whole_numbers(store: AppStore):
    habit = next(h for h in store.habits() if h.type == "scale")
    store.set_habit_value("2026-07-01", habit.id, 3)
    with pytest.raises(StoreError, match="between 1 and 5"):
        store.set_habit_value("2026-07-01", habit.id, 9)
    with pytest.raises(StoreError, match="whole numbers"):
        store.set_habit_value("2026-07-01", habit.id, 2.5)


def test_number_habit_accepts_decimals_and_honours_bounds(store: AppStore):
    habit = store.create_habit(key="water", label="Water", type_="number",
                               unit="L", min_value=0, max_value=10)
    store.set_habit_value("2026-07-01", habit.id, 2.4)
    assert store.journal_range("2026-07-01", "2026-07-01")["2026-07-01"]["values"]["water"] == 2.4
    with pytest.raises(StoreError, match="between"):
        store.set_habit_value("2026-07-01", habit.id, 99)


def test_unbounded_number_habit_accepts_anything_finite(store: AppStore):
    habit = store.create_habit(key="steps_manual", label="Steps", type_="number")
    store.set_habit_value("2026-07-01", habit.id, -12.5)
    store.set_habit_value("2026-07-02", habit.id, 1e6)


def test_non_finite_values_are_rejected(store: AppStore):
    habit = store.create_habit(key="x", label="X", type_="number")
    for bad in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(StoreError):
            store.set_habit_value("2026-07-01", habit.id, bad)


def test_non_numeric_value_is_rejected(store: AppStore):
    habit = store.create_habit(key="x", label="X", type_="number")
    with pytest.raises(StoreError, match="needs a number"):
        store.set_habit_value("2026-07-01", habit.id, "lots")


# --- the unset/zero distinction --------------------------------------------


def test_clearing_a_value_is_not_the_same_as_recording_zero(store: AppStore):
    """The distinction Phase 5's correlations depend on."""
    habit = next(h for h in store.habits() if h.type == "bool")

    store.set_habit_value("2026-07-01", habit.id, 0)
    day = store.journal_day("2026-07-01")
    entry = next(h for h in day["habits"] if h["id"] == habit.id)
    assert entry["value"] == 0.0            # explicit "no"
    assert day["entry_count"] == 1

    store.set_habit_value("2026-07-01", habit.id, None)
    day = store.journal_day("2026-07-01")
    entry = next(h for h in day["habits"] if h["id"] == habit.id)
    assert entry["value"] is None           # not logged at all
    assert day["entry_count"] == 0


def test_setting_a_value_twice_updates_rather_than_duplicates(store: AppStore):
    habit = next(h for h in store.habits() if h.type == "scale")
    store.set_habit_value("2026-07-01", habit.id, 2)
    store.set_habit_value("2026-07-01", habit.id, 4)
    values = store.journal_range("2026-07-01", "2026-07-01")["2026-07-01"]["values"]
    assert list(values.values()) == [4.0]


# --- notes -----------------------------------------------------------------


def test_note_round_trips(store: AppStore):
    store.set_note("2026-07-01", "felt rough")
    assert store.journal_day("2026-07-01")["note"] == "felt rough"


def test_blank_note_is_removed_not_stored(store: AppStore):
    store.set_note("2026-07-01", "something")
    store.set_note("2026-07-01", "   ")
    assert store.journal_day("2026-07-01")["note"] == ""
    assert store.stats()["day_notes"] == 0


def test_journalled_days_covers_notes_and_values(store: AppStore):
    habit = store.habits()[0]
    store.set_habit_value("2026-07-01", habit.id, 1)
    store.set_note("2026-07-03", "note only")
    assert store.journalled_days("2026-07-01", "2026-07-31") == {"2026-07-01", "2026-07-03"}


def test_backfilling_a_past_day_works(store: AppStore):
    habit = store.habits()[0]
    store.set_habit_value("2020-01-01", habit.id, 1)
    assert store.journal_day("2020-01-01")["entry_count"] == 1


# --- workouts --------------------------------------------------------------


def test_create_and_list_workout(store: AppStore):
    w = store.create_workout(day="2026-07-01", type_="Run", duration_s=1800, exertion=6)
    assert w["type"] == "Run" and w["source"] == "manual"
    assert len(store.workouts("2026-07-01", "2026-07-01")) == 1


def test_workout_validation(store: AppStore):
    with pytest.raises(StoreError, match="needs a type"):
        store.create_workout(day="2026-07-01", type_="  ", duration_s=60)
    with pytest.raises(StoreError, match="greater than zero"):
        store.create_workout(day="2026-07-01", type_="Run", duration_s=0)
    with pytest.raises(StoreError, match="1-10"):
        store.create_workout(day="2026-07-01", type_="Run", duration_s=60, exertion=11)


def test_update_and_delete_workout(store: AppStore):
    w = store.create_workout(day="2026-07-01", type_="Run", duration_s=1800)
    updated = store.update_workout(w["id"], type_="Ride", notes="windy")
    assert updated["notes"] == "windy"
    assert updated["type"] == "Run"          # type_ is not an updatable field name
    store.update_workout(w["id"], type="Ride")
    assert store.workout(w["id"])["type"] == "Ride"
    store.delete_workout(w["id"])
    with pytest.raises(StoreError):
        store.workout(w["id"])


def test_confirming_the_same_suggestion_twice_is_refused(store: AppStore):
    store.create_workout(day="2026-07-01", type_="Run", duration_s=1800,
                         source="confirmed", suggestion_key="w123")
    with pytest.raises(StoreError, match="already been logged"):
        store.create_workout(day="2026-07-01", type_="Run", duration_s=1800,
                             source="confirmed", suggestion_key="w123")


def test_dismissals_persist_and_can_be_undone(store: AppStore):
    store.dismiss_suggestion("w1", "2026-07-01")
    store.dismiss_suggestion("w1", "2026-07-01")          # idempotent
    assert store.dismissed_keys("2026-07-01", "2026-07-01") == {"w1"}
    store.undismiss_suggestion("w1")
    assert store.dismissed_keys("2026-07-01", "2026-07-01") == set()


def test_logged_spans_only_include_timed_workouts(store: AppStore):
    store.create_workout(day="2026-07-01", type_="Run", duration_s=600)   # no times
    store.create_workout(day="2026-07-01", type_="Ride", duration_s=600,
                         start_ts=1000, end_ts=1600)
    assert store.logged_spans("2026-07-01", "2026-07-01") == [(1000, 1600)]


# --- export / isolation ----------------------------------------------------


def test_export_includes_every_owned_table(store: AppStore):
    habit = store.habits()[0]
    store.set_habit_value("2026-07-01", habit.id, 1)
    store.set_note("2026-07-01", "hello")
    store.create_workout(day="2026-07-01", type_="Run", duration_s=600)
    store.dismiss_suggestion("w9", "2026-07-01")

    dump = store.export_all()
    assert set(dump) == {"habits", "habit_entries", "day_notes", "workouts",
                         "dismissed_suggestions"}
    assert all(len(rows) >= 1 for rows in dump.values())


def test_store_is_a_separate_file_from_noops(tmp_path: Path):
    """The isolation guarantee: nothing here touches NOOP's database."""
    noop = tmp_path / "noop.sqlite3"
    conn = sqlite3.connect(noop)
    conn.execute("CREATE TABLE daily_metrics (day TEXT)")
    conn.commit()
    conn.close()
    before = noop.stat().st_mtime_ns, noop.stat().st_size

    s = AppStore(tmp_path / "app.sqlite3")
    s.ensure_ready()
    s.create_workout(day="2026-07-01", type_="Run", duration_s=600)
    s.set_note("2026-07-01", "hi")

    assert s.path != noop
    assert (noop.stat().st_mtime_ns, noop.stat().st_size) == before


def test_workout_history_is_ordered_by_day_regardless_of_timestamps(store: AppStore):
    """Manual entries have no start_ts; they must still sort chronologically."""
    store.create_workout(day="2026-07-09", type_="Trail run", duration_s=3840)
    store.create_workout(day="2026-07-16", type_="Long ride", duration_s=5700)
    store.create_workout(day="2026-07-12", type_="Barbell", duration_s=3000)
    store.create_workout(day="2026-07-20", type_="Intervals", duration_s=3180,
                         start_ts=1784000000, end_ts=1784003180, source="confirmed",
                         suggestion_key="w1")

    days = [w["day"] for w in store.workouts("2026-07-01", "2026-07-31")]
    assert days == ["2026-07-20", "2026-07-16", "2026-07-12", "2026-07-09"]
