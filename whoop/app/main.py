"""FastAPI backend. Local-first: binds to the LAN, talks to nothing external.

Phase 1 scope: read-only Today view over NOOP's database.
Trends (Phase 2), journal/workouts (Phase 3), BLE alarms (Phase 4) and PWA
packaging/export (Phase 5) are not implemented yet; endpoints that would serve
them are absent rather than stubbed with fake data.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import settings
from .metrics_meta import DISCLAIMER, METRIC_META, TODAY_TILES
from .noop_adapter import (
    NoopAdapter,
    NoopDBError,
    NoopDBUnavailable,
    day_bounds_utc,
    stage_totals,
    utc_day_key,
)

app = FastAPI(
    title="WHOOP local dashboard",
    description="Local-first personal dashboard over NOOP's on-device data. "
                "No cloud, no accounts, no WHOOP servers.",
    version="0.1.0-phase1",
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
)

adapter = NoopAdapter(settings.noop_db_path, settings.schema_map_path)


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
        "phase": 1,
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
    return {"metrics": METRIC_META, "today_tiles": list(TODAY_TILES), "disclaimer": DISCLAIMER}


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


@app.get("/api/strap/state")
def strap_state() -> dict[str, Any]:
    """Strap BLE connection state.

    Phase 1 holds no BLE connection at all, by design — the strap can only be
    bonded to one host at a time (BLE_NOTES.md 4.1), so the dashboard stays off
    the radio entirely until Phase 4. Reported honestly rather than faked.
    """
    return {
        "state": "idle",
        "implemented": False,
        "message": "BLE lands in Phase 4. Nothing is connected to the strap; "
                   "NOOP keeps the bond.",
        "constraint": "One host may hold the strap's bond at a time. This dashboard "
                      "will connect on demand, send, and disconnect.",
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
