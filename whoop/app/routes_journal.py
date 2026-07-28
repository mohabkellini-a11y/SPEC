"""Journal, habit-definition and workout endpoints (Phase 3).

These write to this app's OWN database (`app/store.py`), never to NOOP's. The
only thing crossing the boundary is the day key and, for workout suggestions,
read-only heart-rate samples.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query
from pydantic import BaseModel, Field

from .config import settings
from .noop_adapter import NoopAdapter, NoopDBError, day_bounds_utc, utc_day_key
from .store import AppStore, StoreError
from .workout_detect import (
    detect,
    filter_suggestions,
    strain_recovery_points,
    weekly_volume,
)

DAY_PATTERN = r"^\d{4}-\d{2}-\d{2}$"

router = APIRouter()
store = AppStore(settings.app_db_path)


def get_store() -> AppStore:
    store.ensure_ready()
    return store


def _today() -> str:
    return utc_day_key(datetime.now(tz=timezone.utc).astimezone(settings.tz))


def _bad_request(exc: StoreError) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))


# --- models ----------------------------------------------------------------


class HabitCreate(BaseModel):
    key: str = Field(min_length=1, max_length=64)
    label: str = Field(min_length=1, max_length=120)
    type: str = Field(description="bool | scale | number")
    unit: str | None = Field(default=None, max_length=24)
    min_value: float | None = None
    max_value: float | None = None


class HabitUpdate(BaseModel):
    label: str | None = Field(default=None, min_length=1, max_length=120)
    type: str | None = None
    unit: str | None = Field(default=None, max_length=24)
    min_value: float | None = None
    max_value: float | None = None
    position: int | None = None
    archived: bool | None = None


class JournalEntry(BaseModel):
    """A partial update: only the keys present are touched.

    `null` for a habit clears it — which is not the same as recording a zero, and
    the store honours that distinction.
    """

    values: dict[int, float | None] | None = None
    note: str | None = None


class WorkoutCreate(BaseModel):
    day: str = Field(pattern=DAY_PATTERN)
    type: str = Field(min_length=1, max_length=64)
    duration_s: int = Field(gt=0, le=86_400)
    exertion: int | None = Field(default=None, ge=1, le=10)
    notes: str | None = Field(default=None, max_length=2000)
    start_ts: int | None = None
    end_ts: int | None = None


class WorkoutUpdate(BaseModel):
    day: str | None = Field(default=None, pattern=DAY_PATTERN)
    type: str | None = Field(default=None, min_length=1, max_length=64)
    duration_s: int | None = Field(default=None, gt=0, le=86_400)
    exertion: int | None = Field(default=None, ge=1, le=10)
    notes: str | None = Field(default=None, max_length=2000)


class SuggestionConfirm(BaseModel):
    type: str = Field(min_length=1, max_length=64)
    exertion: int | None = Field(default=None, ge=1, le=10)
    notes: str | None = Field(default=None, max_length=2000)


# --- habit definitions -----------------------------------------------------


@router.get("/api/habits")
def list_habits(include_archived: bool = Query(False)) -> dict[str, Any]:
    """The habits you have defined. Seeded once on first run, then yours."""
    db = get_store()
    return {
        "habits": [h.as_dict() for h in db.habits(include_archived=include_archived)],
        "types": {
            "bool": "Yes / no",
            "scale": "1-5 scale",
            "number": "Free number, with an optional unit",
        },
    }


@router.post("/api/habits", status_code=201)
def create_habit(payload: HabitCreate) -> dict[str, Any]:
    try:
        return get_store().create_habit(
            key=payload.key, label=payload.label, type_=payload.type,
            unit=payload.unit, min_value=payload.min_value, max_value=payload.max_value,
        ).as_dict()
    except StoreError as exc:
        raise _bad_request(exc) from exc


@router.patch("/api/habits/{habit_id}")
def update_habit(habit_id: int, payload: HabitUpdate) -> dict[str, Any]:
    try:
        return get_store().update_habit(habit_id, **payload.model_dump(exclude_unset=True)).as_dict()
    except StoreError as exc:
        raise _bad_request(exc) from exc


@router.delete("/api/habits/{habit_id}")
def delete_habit(habit_id: int, purge: bool = Query(False, description="Delete logged values too")) -> dict[str, Any]:
    """Archives by default. `purge=true` hard-deletes the habit and its history."""
    db = get_store()
    try:
        if purge:
            db.delete_habit(habit_id)
            return {"deleted": True, "habit_id": habit_id, "history_removed": True}
        habit = db.update_habit(habit_id, archived=True)
        return {"deleted": False, "archived": True, "habit": habit.as_dict(),
                "note": "History kept. Pass purge=true to remove it as well."}
    except StoreError as exc:
        raise _bad_request(exc) from exc


# --- journal ---------------------------------------------------------------


@router.get("/api/journal/{day}")
def get_journal(day: str) -> dict[str, Any]:
    """One day's habits and note. Every active habit appears, valued or not."""
    _validate_day(day)
    db = get_store()
    payload = db.journal_day(day)
    payload["is_today"] = day == _today()
    return payload


@router.put("/api/journal/{day}")
def put_journal(day: str, payload: JournalEntry) -> dict[str, Any]:
    """Partial update, built for one tap at a time.

    The UI writes each change as it happens rather than behind a Save button —
    which is most of how a day's entry stays under fifteen seconds.
    """
    _validate_day(day)
    db = get_store()
    try:
        for habit_id, value in (payload.values or {}).items():
            db.set_habit_value(day, int(habit_id), value)
        if payload.note is not None:
            db.set_note(day, payload.note)
    except StoreError as exc:
        raise _bad_request(exc) from exc
    return db.journal_day(day)


@router.get("/api/journal")
def journal_range(
    days: int = Query(30, ge=1, le=730),
    end: str | None = Query(None, pattern=DAY_PATTERN),
) -> dict[str, Any]:
    """Journal over a window, plus which days have anything logged."""
    db = get_store()
    end_day = end or _today()
    _validate_day(end_day)
    start_day = (date.fromisoformat(end_day) - timedelta(days=days - 1)).isoformat()
    return {
        "start_day": start_day,
        "end_day": end_day,
        "entries": db.journal_range(start_day, end_day),
        "logged_days": sorted(db.journalled_days(start_day, end_day)),
        "habits": [h.as_dict() for h in db.habits(include_archived=True)],
    }


# --- workouts --------------------------------------------------------------


@router.get("/api/workouts")
def list_workouts(
    days: int = Query(90, ge=1, le=730),
    end: str | None = Query(None, pattern=DAY_PATTERN),
) -> dict[str, Any]:
    db = get_store()
    end_day = end or _today()
    _validate_day(end_day)
    start_day = (date.fromisoformat(end_day) - timedelta(days=days - 1)).isoformat()
    return {
        "start_day": start_day,
        "end_day": end_day,
        "workouts": db.workouts(start_day, end_day),
    }


@router.post("/api/workouts", status_code=201)
def create_workout(payload: WorkoutCreate) -> dict[str, Any]:
    try:
        return get_store().create_workout(
            day=payload.day, type_=payload.type, duration_s=payload.duration_s,
            exertion=payload.exertion, notes=payload.notes,
            start_ts=payload.start_ts, end_ts=payload.end_ts, source="manual",
        )
    except StoreError as exc:
        raise _bad_request(exc) from exc


@router.patch("/api/workouts/{workout_id}")
def update_workout(workout_id: int, payload: WorkoutUpdate) -> dict[str, Any]:
    try:
        return get_store().update_workout(workout_id, **payload.model_dump(exclude_unset=True))
    except StoreError as exc:
        raise _bad_request(exc) from exc


@router.delete("/api/workouts/{workout_id}")
def delete_workout(workout_id: int) -> dict[str, Any]:
    try:
        get_store().delete_workout(workout_id)
    except StoreError as exc:
        raise _bad_request(exc) from exc
    return {"deleted": True, "workout_id": workout_id}


# --- suggestions -----------------------------------------------------------


def _adapter() -> NoopAdapter:
    return NoopAdapter(settings.noop_db_path, settings.schema_map_path)


@router.get("/api/workouts/suggestions")
def workout_suggestions(
    days: int = Query(7, ge=1, le=60),
    end: str | None = Query(None, pattern=DAY_PATTERN),
) -> dict[str, Any]:
    """Elevated-HR blocks in NOOP's data that are not yet logged or dismissed.

    Suggestions only. Nothing here is in your log until you confirm it.
    """
    db = get_store()
    adapter = _adapter()
    end_day = end or _today()
    _validate_day(end_day)
    start_day = (date.fromisoformat(end_day) - timedelta(days=days - 1)).isoformat()

    try:
        smap = adapter.schema()
    except NoopDBError as exc:
        return {"available": False, "reason": str(exc), "suggestions": [],
                "start_day": start_day, "end_day": end_day}
    if smap.hr is None:
        return {
            "available": False,
            "reason": "No heart-rate table resolved in this NOOP database, so there is "
                      "nothing to detect bouts in.",
            "suggestions": [], "start_day": start_day, "end_day": end_day,
        }

    dismissed = db.dismissed_keys(start_day, end_day)
    confirmed = db.confirmed_keys(start_day, end_day)
    logged = db.logged_spans(start_day, end_day)

    found = []
    day_cursor = date.fromisoformat(start_day)
    last = date.fromisoformat(end_day)
    while day_cursor <= last:
        key = day_cursor.isoformat()
        start_ts, end_ts = day_bounds_utc(key)
        samples = adapter.heart_rate_range(start_ts, end_ts - 1, max_points=5000)
        found.extend(detect(samples, key))
        day_cursor += timedelta(days=1)

    visible = filter_suggestions(found, dismissed, confirmed, logged)
    return {
        "available": True,
        "start_day": start_day,
        "end_day": end_day,
        "suggestions": [s.as_dict() for s in sorted(visible, key=lambda s: s.start_ts, reverse=True)],
        "n_detected": len(found),
        "n_hidden": len(found) - len(visible),
        "method_note": "Elevated-HR blocks only. NOOP's own detector also uses the "
                       "motion channel; without it, this one requires a bout to sit "
                       "above the day's median HR and to average 30 bpm above resting. "
                       "That keeps false positives down but WILL miss easy sessions "
                       "(a walk, gentle yoga) — log those by hand.",
    }


@router.post("/api/workouts/suggestions/{key}/confirm", status_code=201)
def confirm_suggestion(key: str, payload: SuggestionConfirm,
                       day: str = Query(..., pattern=DAY_PATTERN)) -> dict[str, Any]:
    """Turn a suggestion into a logged workout, carrying its HR detail across."""
    db = get_store()
    adapter = _adapter()
    start_ts, end_ts = day_bounds_utc(day)
    try:
        samples = adapter.heart_rate_range(start_ts, end_ts - 1, max_points=5000)
    except NoopDBError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    match = next((s for s in detect(samples, day) if s.key == key), None)
    if match is None:
        raise HTTPException(
            status_code=404,
            detail=f"No suggestion '{key}' on {day}. It may have changed shape as more "
                   "samples arrived — reload the suggestions.",
        )
    try:
        return db.create_workout(
            day=day, type_=payload.type, duration_s=match.duration_s,
            exertion=payload.exertion, notes=payload.notes,
            start_ts=match.start_ts, end_ts=match.end_ts,
            source="confirmed", suggestion_key=match.key,
            avg_hr=match.avg_hr, peak_hr=match.peak_hr,
        )
    except StoreError as exc:
        raise _bad_request(exc) from exc


@router.post("/api/workouts/suggestions/{key}/dismiss")
def dismiss_suggestion(key: str, day: str = Query(..., pattern=DAY_PATTERN)) -> dict[str, Any]:
    get_store().dismiss_suggestion(key, day)
    return {"dismissed": True, "key": key, "day": day}


@router.delete("/api/workouts/suggestions/{key}/dismiss")
def undismiss_suggestion(key: str) -> dict[str, Any]:
    get_store().undismiss_suggestion(key)
    return {"dismissed": False, "key": key}


# --- summary ---------------------------------------------------------------


@router.get("/api/workouts/summary")
def workouts_summary(
    days: int = Query(90, ge=7, le=730),
    end: str | None = Query(None, pattern=DAY_PATTERN),
) -> dict[str, Any]:
    """Weekly volume and the strain-vs-recovery scatter."""
    db = get_store()
    end_day = end or _today()
    _validate_day(end_day)
    start_day = (date.fromisoformat(end_day) - timedelta(days=days - 1)).isoformat()

    workouts = db.workouts(start_day, end_day)

    points: list[dict[str, Any]] = []
    scatter_available = True
    scatter_reason = None
    try:
        adapter = _adapter()
        if adapter.schema().resolved:
            points = strain_recovery_points(adapter.daily_range(start_day, end_day))
        else:
            scatter_available = False
            scatter_reason = "NOOP's daily-metrics table could not be resolved."
    except NoopDBError as exc:
        scatter_available = False
        scatter_reason = str(exc)

    total_min = sum((w.get("duration_s") or 0) for w in workouts) / 60.0
    return {
        "start_day": start_day,
        "end_day": end_day,
        "n_workouts": len(workouts),
        "total_minutes": round(total_min, 1),
        "weekly_volume": weekly_volume(workouts, start_day, end_day),
        "strain_recovery": {
            "available": scatter_available,
            "reason": scatter_reason,
            "points": points,
            "note": "Each point is one day with both a strain and a recovery score. "
                    "Days missing either are omitted rather than plotted at zero. "
                    "Descriptive — it shows what happened, not what caused it.",
        },
    }


@router.get("/api/store/stats")
def store_stats() -> dict[str, Any]:
    """What lives in this app's own database. Shown in diagnostics."""
    return get_store().stats()


def _validate_day(day: str) -> None:
    try:
        date.fromisoformat(day)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"'{day}' is not a YYYY-MM-DD date") from exc
