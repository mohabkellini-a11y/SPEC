"""FastAPI backend. Local-first: binds to the LAN, talks to nothing external.

Phase 1-3 scope: read-only Today view and trends over NOOP's database, plus a
habit journal and workout log in this app's OWN database.
BLE alarms (Phase 4) and PWA packaging/export/correlations (Phase 5) are not
implemented yet; endpoints that would serve them are absent rather than stubbed
with fake data.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .analytics import align, build_series, dense_days
from .config import settings
from .metrics_meta import (
    DISCLAIMER,
    LOWER_IS_BETTER,
    METRIC_META,
    NEUTRAL_DIRECTION,
    TODAY_TILES,
    TREND_METRICS,
)
from .routes_alarms import get_scheduler, router as alarms_router
from .routes_journal import get_store, router as journal_router
from .noop_adapter import (
    NoopAdapter,
    NoopDBError,
    NoopDBUnavailable,
    day_bounds_utc,
    stage_totals,
    utc_day_key,
)

@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Start the alarm scheduler with the server, stop it with the server.

    A pending alarm survives a restart because it lives in SQLite — and, once
    armed, on the strap itself. Startup reconciles: anything whose time passed
    while we were down is marked fired or failed rather than silently retried.
    """
    scheduler = get_scheduler()
    try:
        await scheduler.run_once()
        await scheduler.start()
    except Exception as exc:  # noqa: BLE001 - reported via /api/health, not fatal
        _app.state.scheduler_error = str(exc)
    yield
    await scheduler.stop()


app = FastAPI(
    title="WHOOP local dashboard",
    description="Local-first personal dashboard over NOOP's on-device data. "
                "No cloud, no accounts, no WHOOP servers.",
    version="0.4.0-phase4",
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
    lifespan=lifespan,
)

adapter = NoopAdapter(settings.noop_db_path, settings.schema_map_path)

app.include_router(journal_router)
app.include_router(alarms_router)


def _clean(row: dict[str, Any] | None) -> dict[str, Any]:
    """Strip adapter bookkeeping keys from a metrics row."""
    if not row:
        return {}
    return {k: v for k, v in row.items() if not k.startswith("_")}


@app.exception_handler(NoopDBUnavailable)
async def _unavailable_handler(_request, exc: NoopDBUnavailable) -> JSONResponse:
    return JSONResponse(status_code=503, content={"error": "noop_db_unavailable", "detail": str(exc)})


@app.exception_handler(NoopDBError)
async def _db_error_handler(_request, exc: NoopDBError) -> JSONResponse:
    return JSONResponse(status_code=500, content={"error": "noop_db_error", "detail": str(exc)})


@app.get("/api/health")
def health() -> dict[str, Any]:
    """Cheap liveness + whether the NOOP database is reachable at all."""
    out: dict[str, Any] = {
        "ok": True,
        "phase": 4,
        "noop_db_path": str(settings.noop_db_path) if settings.noop_db_path else None,
        "noop_db_configured": settings.noop_db_path is not None,
        "noop_db_present": settings.noop_db_exists,
    }
    try:
        smap = adapter.schema()
        out["schema_resolved"] = smap.resolved
        out["day_count"] = adapter.day_count() if smap.resolved else 0
    except NoopDBError as exc:
        out["schema_resolved"] = False
        out["error"] = str(exc)

    # The app's own database is independent of NOOP's: the journal keeps working
    # when NOOP's file is missing, and vice versa. get_store() migrates on first
    # touch, so health does not depend on a startup hook having fired.
    try:
        out["app_db"] = {"ok": True, **get_store().stats()}
    except Exception as exc:                      # noqa: BLE001 - reported, not raised
        out["app_db"] = {"ok": False, "error": str(exc)}
    return out


@app.get("/api/diagnostics")
def diagnostics() -> dict[str, Any]:
    """What the adapter found in the database. The first thing to check when a
    tile is empty — it distinguishes 'no data' from 'column not resolved'."""
    smap = adapter.schema(refresh=True)
    return {
        "db_path": str(settings.noop_db_path) if settings.noop_db_path else None,
        "schema": smap.describe(),
        "schema_note": "NOOP's persistence layer is not published; this schema was "
                       "resolved at runtime. See SCHEMA_NOTES.md.",
    }


@app.get("/api/metrics/meta")
def metrics_meta() -> dict[str, Any]:
    """Label, unit, method and approximation status for every metric."""
    return {"metrics": METRIC_META,
            "today_tiles": list(TODAY_TILES),
            "trend_metrics": list(TREND_METRICS),
            "lower_is_better": sorted(LOWER_IS_BETTER),
            "neutral_direction": sorted(NEUTRAL_DIRECTION),
            "disclaimer": DISCLAIMER}


@app.get("/api/today")
def today(
    day: str | None = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$",
                            description="UTC day key. Defaults to today."),
    fallback: bool = Query(True, description="If the day has no row, return the most recent one."),
) -> dict[str, Any]:
    """Today's metrics, plus everything needed to render an honest empty state."""
    now = datetime.now(tz=timezone.utc)
    requested = day or utc_day_key(now.astimezone(settings.tz))

    smap = adapter.schema()
    if not smap.resolved:
        raise HTTPException(
            status_code=503,
            detail={
                "error": "schema_unresolved",
                "message": "Could not identify NOOP's daily-metrics table.",
                "tables_found": sorted(smap.all_tables),
                "notes": smap.notes,
                "next_step": "Run `python -m app.probe` and pin the names in schema_map.json.",
            },
        )

    row = adapter.daily(requested)
    resolved_day = requested
    latest = adapter.latest_day()
    fell_back = False

    if row is None and fallback and latest:
        resolved_day = latest
        row = adapter.daily(latest)
        fell_back = True

    # Signed: positive when the data we found is older than the day asked for,
    # negative when the user has navigated back past the start of the record.
    # Staleness is `fell_back`, not the sign of this number — browsing to a past
    # day is not the same thing as the strap being behind.
    days_behind = 0
    if row is not None:
        days_behind = (datetime.fromisoformat(requested).date()
                       - datetime.fromisoformat(resolved_day).date()).days

    # Sleep detail for the resolved day. NOOP attributes a session to the day its
    # END falls on (SCHEMA_NOTES.md 1.4), so look back a day for the in-bed span.
    sleep: dict[str, Any] = {"sessions": [], "stage_minutes": {}, "available": smap.sleep is not None}
    if smap.sleep is not None and resolved_day:
        start_ts, end_ts = day_bounds_utc(resolved_day)
        sessions = adapter.sleep_sessions(start_ts - 86400, end_ts)
        sessions = [s for s in sessions if start_ts <= int(s["end_ts"]) < end_ts]
        merged: dict[str, float] = {}
        for session in sessions:
            for stage, minutes in stage_totals(session.get("stages", [])).items():
                merged[stage] = merged.get(stage, 0.0) + minutes
        sleep["sessions"] = sessions
        sleep["stage_minutes"] = {k: round(v, 1) for k, v in merged.items()}

    last_hr = adapter.last_heart_rate()
    hr_block = {
        "available": smap.hr is not None,
        "last": last_hr,
        "age_seconds": int(now.timestamp()) - last_hr["ts"] if last_hr else None,
    }

    battery = adapter.battery()
    metrics = _clean(row)

    return {
        "requested_day": requested,
        "resolved_day": resolved_day if row else None,
        "latest_day": latest,
        "days_behind": days_behind,
        "fell_back": fell_back,
        "is_stale": fell_back and days_behind > 0,
        "has_data": row is not None,
        "metrics": metrics,
        # recovery is nil during NOOP's 4-night cold start rather than zero.
        "calibrating": bool(row) and metrics.get("recovery") is None,
        "sleep": sleep,
        "heart_rate": hr_block,
        "battery": battery,
        "battery_available": battery is not None,
        "unavailable_fields": (row or {}).get("_unavailable", []),
        "suspect_values": (row or {}).get("_suspect", []),
        "generated_at": now.isoformat(),
        "disclaimer": DISCLAIMER,
    }


@app.get("/api/heart-rate")
def heart_rate(
    day: str | None = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    max_points: int = Query(720, ge=50, le=5000),
) -> dict[str, Any]:
    """HR samples for a day, decimated. Used for the Today sparkline."""
    smap = adapter.schema()
    if smap.hr is None:
        return {"available": False, "samples": [], "reason": "no heart-rate table resolved"}
    key = day or utc_day_key(datetime.now(tz=timezone.utc))
    start_ts, end_ts = day_bounds_utc(key)
    return {
        "available": True,
        "day": key,
        "samples": adapter.heart_rate_range(start_ts, end_ts - 1, max_points),
    }


@app.get("/api/trends")
def trends(
    days: int = Query(30, ge=2, le=730, description="Window length in days, ending today."),
    metrics: str | None = Query(None, description="Comma-separated metric keys."),
    end: str | None = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$",
                            description="Last day of the window. Defaults to today."),
    rolling: int = Query(7, ge=2, le=90, description="Rolling-mean window in days."),
) -> dict[str, Any]:
    """Daily series for the trend charts, densified so gaps stay visible.

    One query per window: the range is read once and every metric is projected
    out of the same rows.
    """
    smap = adapter.schema()
    if not smap.resolved:
        raise HTTPException(
            status_code=503,
            detail={"error": "schema_unresolved",
                    "message": "Could not identify NOOP's daily-metrics table.",
                    "tables_found": sorted(smap.all_tables),
                    "next_step": "Run `python -m app.probe`."},
        )

    requested = [m.strip() for m in metrics.split(",")] if metrics else list(TREND_METRICS)
    unknown = [m for m in requested if m not in METRIC_META]
    if unknown:
        raise HTTPException(status_code=400,
                            detail=f"Unknown metric(s): {', '.join(unknown)}")
    # Only daily-record fields can be charted; live readings have no history.
    unchartable = [m for m in requested if m not in TREND_METRICS and m in {"heart_rate", "battery"}]
    if unchartable:
        raise HTTPException(
            status_code=400,
            detail=f"{', '.join(unchartable)} is a live reading with no daily history.",
        )

    end_day = end or utc_day_key(datetime.now(tz=timezone.utc).astimezone(settings.tz))
    start_day = (datetime.fromisoformat(end_day).date() - timedelta(days=days - 1)).isoformat()
    day_keys = dense_days(start_day, end_day)

    rows = adapter.daily_range(start_day, end_day)
    # A field absent from this NOOP database is reported as such, not charted flat.
    resolved_fields = set(smap.daily.columns)

    series: dict[str, Any] = {}
    for metric in requested:
        if metric not in resolved_fields:
            series[metric] = {
                "metric": metric, "has_data": False, "unavailable": True,
                "reason": "not a column in this NOOP database",
                "days": day_keys, "values": [None] * len(day_keys), "rolling": [],
                "summary": None, "delta": None, "slope_per_day": None,
            }
            continue
        built = build_series(
            metric, day_keys, align(rows, day_keys, metric),
            rolling_window=rolling,
            # Half the window against the other half, so a 30-day view compares
            # the last 15 days with the 15 before them.
            delta_window=max(1, days // 2),
        )
        payload = built.as_dict()
        payload["unavailable"] = False
        series[metric] = payload

    return {
        "start_day": start_day,
        "end_day": end_day,
        "days": days,
        "rolling_window": rolling,
        "rows_found": len(rows),
        "series": series,
        "direction": {
            "lower_is_better": sorted(LOWER_IS_BETTER),
            "neutral": sorted(NEUTRAL_DIRECTION),
        },
        "note": "Descriptive statistics over NOOP's already-approximate daily values. "
                "Gaps are shown as gaps and are never interpolated. A trend line "
                "describes the past; it is not a forecast or a cause.",
        "disclaimer": DISCLAIMER,
    }


# --- static SPA -------------------------------------------------------------

if settings.web_dir.is_dir():
    app.mount("/static", StaticFiles(directory=settings.web_dir), name="static")

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(settings.web_dir / "index.html")


def run() -> None:
    import uvicorn

    uvicorn.run("app.main:app", host=settings.host, port=settings.port, reload=False)


if __name__ == "__main__":
    run()
