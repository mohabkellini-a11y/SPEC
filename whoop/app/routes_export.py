"""Correlations and export (Phase 5).

Export is deliberately complete and boring: everything this app owns, plus
everything it reads from NOOP, in CSV or JSON, with no server-side filtering
you cannot see. It is your data; the point is to be able to leave.
"""

from __future__ import annotations

import csv
import io
import json
import zipfile
from datetime import date, datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response, StreamingResponse

from .analytics import dense_days
from .config import settings
from .correlations import compare
from .metrics_meta import DISCLAIMER, METRIC_META, TREND_METRICS
from .noop_adapter import NoopAdapter, NoopDBError, day_bounds_utc
from .routes_journal import get_store

router = APIRouter()

DAY_PATTERN = r"^\d{4}-\d{2}-\d{2}$"

#: Metrics worth correlating a habit against. Live readings are excluded — they
#: have no per-day value to compare.
CORRELATABLE = TREND_METRICS + ("efficiency", "resp_rate_bpm", "skin_temp_dev_c")


def _adapter() -> NoopAdapter:
    return NoopAdapter(settings.noop_db_path, settings.schema_map_path)


def _today() -> str:
    return datetime.now(tz=timezone.utc).astimezone(settings.tz).strftime("%Y-%m-%d")


# --- correlations -----------------------------------------------------------


@router.get("/api/correlations/options")
def correlation_options() -> dict[str, Any]:
    """What can be correlated against what, for the pickers."""
    store = get_store()
    return {
        "habits": [h.as_dict() for h in store.habits(include_archived=True)],
        "metrics": [
            {"key": key, "label": METRIC_META[key]["label"],
             "unit": METRIC_META[key].get("unit", ""),
             "kind": METRIC_META[key]["kind"]}
            for key in CORRELATABLE if key in METRIC_META
        ],
        "default_lag": 1,
        "lag_note": (
            "Lag 1 is the default because NOOP files a night's sleep under the "
            "day it ends: Friday's habit lines up with Saturday's recovery."
        ),
    }


@router.get("/api/correlations")
def correlation(
    habit_id: int = Query(..., description="Which habit to compare"),
    metric: str = Query("recovery", description="Which NOOP metric"),
    days: int = Query(90, ge=7, le=730),
    lag: int = Query(1, ge=0, le=7),
    end: str | None = Query(None, pattern=DAY_PATTERN),
) -> dict[str, Any]:
    """Compare one habit against one metric. Descriptive only, never causal."""
    store = get_store()
    if metric not in METRIC_META:
        raise HTTPException(status_code=400, detail=f"Unknown metric '{metric}'.")
    if metric not in CORRELATABLE:
        raise HTTPException(
            status_code=400,
            detail=f"'{metric}' is a live reading with no daily value to correlate.")

    habits = {h.id: h for h in store.habits(include_archived=True)}
    habit = habits.get(habit_id)
    if habit is None:
        raise HTTPException(status_code=404, detail=f"No habit with id {habit_id}.")

    end_day = end or _today()
    start_day = (date.fromisoformat(end_day) - timedelta(days=days - 1)).isoformat()

    journal = store.journal_range(start_day, end_day)
    habit_by_day = {
        day: entry["values"][habit.key]
        for day, entry in journal.items()
        if habit.key in entry["values"]
    }

    adapter = _adapter()
    try:
        if not adapter.schema().resolved:
            raise HTTPException(
                status_code=503,
                detail="NOOP's daily-metrics table could not be resolved, so there "
                       "is nothing to correlate against. Run `python -m app.probe`.")
        # The metric window must extend past the habit window by the lag.
        rows = adapter.daily_range(start_day, (date.fromisoformat(end_day)
                                               + timedelta(days=lag)).isoformat())
    except NoopDBError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    metric_by_day = {r["day"]: r.get(metric) for r in rows}
    meta = METRIC_META[metric]

    result = compare(
        habit_by_day, metric_by_day,
        habit_type=habit.type, habit_label=habit.label,
        metric_label=meta["label"], metric_unit=meta.get("unit", ""), lag=lag,
    )
    result.update({
        "habit_id": habit.id,
        "habit_key": habit.key,
        "metric": metric,
        "metric_kind": meta["kind"],
        "metric_method": meta.get("method"),
        "start_day": start_day,
        "end_day": end_day,
        "days_requested": days,
        "journalled_days": len(habit_by_day),
        "disclaimer": DISCLAIMER,
    })
    # The overlay chart wants a dense timeline, gaps and all.
    timeline = dense_days(start_day, end_day)
    habit_series = [habit_by_day.get(d) for d in timeline]
    metric_series = [metric_by_day.get(d) for d in timeline]
    result["timeline"] = {"days": timeline, "habit": habit_series,
                          "metric": metric_series}
    return result


# --- export -----------------------------------------------------------------


def _noop_rows(start_day: str, end_day: str) -> dict[str, list[dict[str, Any]]]:
    """Everything readable from NOOP in the window. Empty if NOOP is unavailable."""
    out: dict[str, list[dict[str, Any]]] = {"noop_daily_metrics": [], "noop_sleep_sessions": []}
    adapter = _adapter()
    try:
        if not adapter.schema().resolved:
            return out
        out["noop_daily_metrics"] = [
            {k: v for k, v in row.items() if not k.startswith("_")}
            for row in adapter.daily_range(start_day, end_day)
        ]
        start_ts, _ = day_bounds_utc(start_day)
        _, end_ts = day_bounds_utc(end_day)
        sessions = adapter.sleep_sessions(start_ts, end_ts)
        for session in sessions:
            row = dict(session)
            row["stages"] = json.dumps(row.get("stages", []))
            out["noop_sleep_sessions"].append(row)
    except NoopDBError:
        pass
    return out


def _gather(start_day: str, end_day: str, include_noop: bool) -> dict[str, Any]:
    store = get_store()
    tables = store.export_all()

    habits_by_id = {h["id"]: h for h in tables["habits"]}
    # Denormalise the journal: a CSV of habit_id -> value is useless on its own.
    journal_rows = []
    for entry in tables["habit_entries"]:
        habit = habits_by_id.get(entry["habit_id"], {})
        journal_rows.append({
            "day": entry["day"],
            "habit_key": habit.get("key"),
            "habit_label": habit.get("label"),
            "habit_type": habit.get("type"),
            "unit": habit.get("unit"),
            "value": entry["value"],
            "updated_at": entry["updated_at"],
        })
    journal_rows.sort(key=lambda r: (r["day"], r["habit_key"] or ""))

    data: dict[str, Any] = {
        "habits": tables["habits"],
        "journal": journal_rows,
        "day_notes": tables["day_notes"],
        "workouts": tables["workouts"],
        "alarms": tables["alarms"],
        "dismissed_suggestions": tables["dismissed_suggestions"],
    }
    if include_noop:
        data.update(_noop_rows(start_day, end_day))
    return data


def _meta(start_day: str, end_day: str, include_noop: bool) -> dict[str, Any]:
    return {
        "exported_at": datetime.now(tz=timezone.utc).isoformat(),
        "window": {"start_day": start_day, "end_day": end_day},
        "includes_noop_data": include_noop,
        "app": "whoop local dashboard",
        "noop_db_path": str(settings.noop_db_path) if settings.noop_db_path else None,
        "note": (
            "Journal, workouts, notes and alarms are yours and were typed by you. "
            "The noop_* tables are read-only copies of values NOOP computed on "
            "your device; every one is an approximation."
        ),
        "disclaimer": DISCLAIMER,
    }


def _csv_bytes(rows: list[dict[str, Any]]) -> bytes:
    buffer = io.StringIO()
    if rows:
        fieldnames: list[str] = []
        for row in rows:
            for key in row:
                if key not in fieldnames:
                    fieldnames.append(key)
        writer = csv.DictWriter(buffer, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    return buffer.getvalue().encode("utf-8")


def _window(days: int, end: str | None) -> tuple[str, str]:
    end_day = end or _today()
    start_day = (date.fromisoformat(end_day) - timedelta(days=days - 1)).isoformat()
    return start_day, end_day


@router.get("/api/export/summary")
def export_summary(days: int = Query(3650, ge=1, le=3650),
                   end: str | None = Query(None, pattern=DAY_PATTERN)) -> dict[str, Any]:
    """Row counts, so the UI can say what a download will contain."""
    start_day, end_day = _window(days, end)
    data = _gather(start_day, end_day, include_noop=True)
    return {
        "window": {"start_day": start_day, "end_day": end_day},
        "counts": {name: len(rows) for name, rows in data.items()},
        "noop_available": bool(data.get("noop_daily_metrics")),
    }


@router.get("/api/export.json")
def export_json(days: int = Query(3650, ge=1, le=3650),
                end: str | None = Query(None, pattern=DAY_PATTERN),
                include_noop: bool = Query(True)) -> Response:
    start_day, end_day = _window(days, end)
    payload = {"meta": _meta(start_day, end_day, include_noop),
               **_gather(start_day, end_day, include_noop)}
    body = json.dumps(payload, indent=2, default=str).encode("utf-8")
    stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%d")
    return Response(
        content=body, media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="strap-export-{stamp}.json"'},
    )


@router.get("/api/export.csv")
def export_csv_zip(days: int = Query(3650, ge=1, le=3650),
                   end: str | None = Query(None, pattern=DAY_PATTERN),
                   include_noop: bool = Query(True)) -> StreamingResponse:
    """One CSV per table, zipped — CSV has no way to hold several tables."""
    start_day, end_day = _window(days, end)
    data = _gather(start_day, end_day, include_noop)

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, rows in data.items():
            archive.writestr(f"{name}.csv", _csv_bytes(rows))
        archive.writestr("README.txt", _readme(start_day, end_day, data))
        archive.writestr("meta.json",
                         json.dumps(_meta(start_day, end_day, include_noop), indent=2))
    buffer.seek(0)
    stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%d")
    return StreamingResponse(
        buffer, media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="strap-export-{stamp}.zip"'},
    )


@router.get("/api/export/{table}.csv")
def export_table_csv(table: str,
                     days: int = Query(3650, ge=1, le=3650),
                     end: str | None = Query(None, pattern=DAY_PATTERN)) -> Response:
    """A single table, for when you want one thing in a spreadsheet."""
    start_day, end_day = _window(days, end)
    data = _gather(start_day, end_day, include_noop=True)
    if table not in data:
        raise HTTPException(status_code=404,
                            detail=f"No such table '{table}'. Available: {', '.join(data)}")
    return Response(
        content=_csv_bytes(data[table]), media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{table}.csv"'},
    )


def _readme(start_day: str, end_day: str, data: dict[str, list]) -> str:
    lines = [
        "Strap — full export",
        "===================",
        "",
        f"Exported {datetime.now(tz=timezone.utc).isoformat()}",
        f"Window: {start_day} to {end_day}",
        "",
        "Files",
        "-----",
    ]
    descriptions = {
        "habits": "the habits you defined",
        "journal": "every habit value you logged, with the habit's name resolved",
        "day_notes": "your free-text note per day",
        "workouts": "your workout log (source=manual or confirmed-from-strap)",
        "alarms": "alarms and timers, with their outcome",
        "dismissed_suggestions": "workout suggestions you rejected",
        "noop_daily_metrics": "read-only copy of NOOP's per-day values",
        "noop_sleep_sessions": "read-only copy of NOOP's sleep sessions",
    }
    for name, rows in data.items():
        lines.append(f"  {name}.csv  ({len(rows)} rows) — {descriptions.get(name, '')}")
    lines += [
        "",
        "Notes",
        "-----",
        "* Timestamps are unix seconds; day keys are UTC calendar days, matching",
        "  how NOOP buckets them.",
        "* In journal.csv a habit with no row for a day was NOT logged. That is",
        "  different from a logged zero, which appears as value=0.0.",
        "* The noop_* files are values NOOP computed on your device. Every one is",
        "  an approximation, not clinical data.",
        "",
        DISCLAIMER,
    ]
    return "\n".join(lines)
