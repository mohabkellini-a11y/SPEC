"""Alarm and timer endpoints (Phase 4).

Every response says what actually happened on the strap, not just what was
recorded here. An alarm that could not be armed comes back with `ok: false` and
a sentence explaining why — the brief's "I need to know if an alarm did NOT get
set" is the design constraint for this whole file.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from .ble.scheduler import AlarmScheduler, describe_alarm
from .ble.transport import StrapMonitor, build_transport
from .config import settings
from .routes_journal import get_store
from .store import StoreError

router = APIRouter()

monitor = StrapMonitor()
transport = build_transport(settings.strap_address, monitor)
scheduler: AlarmScheduler | None = None

MAX_AHEAD_SECONDS = 60 * 60 * 24 * 30      # a month; the strap stores a u32 time
MIN_AHEAD_SECONDS = 5


def get_scheduler() -> AlarmScheduler:
    global scheduler
    if scheduler is None:
        scheduler = AlarmScheduler(get_store(), transport, monitor)
    return scheduler


class AlarmCreate(BaseModel):
    """Either an absolute time or an offset — a nap timer is just the latter."""

    fire_at: int | None = Field(default=None, description="Absolute unix seconds.")
    in_seconds: int | None = Field(default=None, ge=MIN_AHEAD_SECONDS,
                                   le=MAX_AHEAD_SECONDS,
                                   description="Offset from now. Use for timers.")
    label: str | None = Field(default=None, max_length=120)
    kind: str = Field(default="alarm", pattern="^(alarm|timer)$")


class AlarmUpdate(BaseModel):
    fire_at: int | None = None
    in_seconds: int | None = Field(default=None, ge=MIN_AHEAD_SECONDS, le=MAX_AHEAD_SECONDS)
    label: str | None = Field(default=None, max_length=120)


def _resolve_fire_at(fire_at: int | None, in_seconds: int | None) -> int:
    now = int(time.time())
    if in_seconds is not None:
        return now + int(in_seconds)
    if fire_at is None:
        raise HTTPException(status_code=422,
                            detail="Provide either fire_at or in_seconds.")
    fire_at = int(fire_at)
    if fire_at <= now + MIN_AHEAD_SECONDS:
        raise HTTPException(
            status_code=422,
            detail=f"That time is in the past or too soon — allow at least "
                   f"{MIN_AHEAD_SECONDS} seconds.")
    if fire_at > now + MAX_AHEAD_SECONDS:
        raise HTTPException(status_code=422, detail="That is more than 30 days away.")
    return fire_at


def _outcome(alarm: dict[str, Any], sched: AlarmScheduler) -> tuple[bool, bool, str]:
    """Separate the three outcomes a new alarm can have.

    Armed, queued behind an earlier alarm (normal — the strap holds one), or
    genuinely failed. Conflating the last two would cry wolf on every second
    alarm and teach you to ignore the warning that actually matters.
    """
    if alarm["state"] == "armed":
        return True, False, "Set on the strap. It will buzz even if this machine is off."

    armed_id = sched.armed_id()
    if armed_id is not None:
        holder = sched.store.alarm(armed_id)
        if int(holder["fire_at"]) < int(alarm["fire_at"]):
            when = datetime.fromtimestamp(int(holder["fire_at"]), tz=timezone.utc)
            return False, True, (
                f"Scheduled. The strap holds one alarm at a time and an earlier one "
                f"({when.strftime('%H:%M')} UTC) has it, so this goes on the strap "
                "after that fires — which needs the dashboard running."
            )

    return False, False, (
        alarm.get("last_error") or "Scheduled here, but NOT set on the strap."
    )


def _decorate(alarm: dict[str, Any], sched: AlarmScheduler) -> dict[str, Any]:
    payload = describe_alarm(alarm, sched.armed_id(), int(time.time()))
    payload["fire_at_iso"] = datetime.fromtimestamp(
        int(alarm["fire_at"]), tz=timezone.utc).isoformat()
    return payload


@router.get("/api/alarms")
def list_alarms(include_done: bool = Query(False, description="Include fired/cancelled")) -> dict[str, Any]:
    sched = get_scheduler()
    states = None if include_done else ("pending", "armed", "failed")
    alarms = sched.store.alarms(states=states)
    return {
        "alarms": [_decorate(a, sched) for a in alarms],
        "armed_alarm_id": sched.armed_id(),
        "now": int(time.time()),
        "one_alarm_limit": (
            "The strap holds one alarm at a time, so only the next one is really "
            "on it. The others are armed in turn as each fires, which needs this "
            "dashboard to be running."
        ),
    }


@router.post("/api/alarms", status_code=201)
async def create_alarm(payload: AlarmCreate) -> dict[str, Any]:
    """Schedule an alarm and immediately try to put it on the strap.

    Returns 201 whether or not arming succeeded — the alarm IS scheduled either
    way — but `armed` and `message` tell you the truth about the strap.
    """
    sched = get_scheduler()
    fire_at = _resolve_fire_at(payload.fire_at, payload.in_seconds)

    try:
        alarm = sched.store.create_alarm(fire_at=fire_at, label=payload.label,
                                         kind=payload.kind)
    except StoreError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # Arm straight away: the strap stores an absolute time, so an armed alarm
    # survives this process dying, the laptop sleeping, or a reboot.
    result = await sched.run_once()
    refreshed = sched.store.alarm(alarm["id"])
    armed, queued, message = _outcome(refreshed, sched)

    return {
        "alarm": _decorate(refreshed, sched),
        "armed": armed,
        "queued": queued,
        # ok means "nothing went wrong" — an alarm correctly waiting its turn
        # behind an earlier one is not a failure and must not be shouted about.
        "ok": armed or queued,
        "message": message,
        "displaced": [a["id"] for a in result.to_demote],
        "strap": monitor.as_dict(transport),
    }


@router.patch("/api/alarms/{alarm_id}")
async def update_alarm(alarm_id: int, payload: AlarmUpdate) -> dict[str, Any]:
    sched = get_scheduler()
    try:
        existing = sched.store.alarm(alarm_id)
    except StoreError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if existing["state"] in ("fired", "cancelled"):
        raise HTTPException(status_code=400,
                            detail="That alarm has already fired or been cancelled.")

    fields: dict[str, Any] = {}
    if payload.label is not None:
        fields["label"] = payload.label
    if payload.fire_at is not None or payload.in_seconds is not None:
        fields["fire_at"] = _resolve_fire_at(payload.fire_at, payload.in_seconds)
        # The time changed, so whatever is on the strap is now wrong. Reset to
        # pending and let the next pass re-arm it.
        fields["state"] = "pending"
        fields["attempts"] = 0
        fields["last_error"] = None

    sched.store.update_alarm(alarm_id, **fields)
    await sched.run_once()
    refreshed = sched.store.alarm(alarm_id)
    armed, queued, message = _outcome(refreshed, sched)
    return {
        "alarm": _decorate(refreshed, sched),
        "armed": armed,
        "queued": queued,
        "ok": armed or queued,
        "message": message,
        "warning": (
            "The old time may still be set on the strap — there is no verified "
            "command to un-set an alarm. See BLE_NOTES.md 3.3."
            if existing["state"] == "armed" and "fire_at" in fields else None
        ),
    }


@router.delete("/api/alarms/{alarm_id}")
async def cancel_alarm(alarm_id: int) -> dict[str, Any]:
    sched = get_scheduler()
    try:
        outcome = await sched.cancel(alarm_id)
    except StoreError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    await sched.run_once()
    return outcome


@router.post("/api/alarms/{alarm_id}/retry")
async def retry_alarm(alarm_id: int) -> dict[str, Any]:
    """Try again after a failure — the obvious thing to want at 6am."""
    sched = get_scheduler()
    try:
        alarm = sched.store.alarm(alarm_id)
    except StoreError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if int(alarm["fire_at"]) <= int(time.time()):
        raise HTTPException(status_code=400, detail="That time has already passed.")

    sched.store.update_alarm(alarm_id, state="pending", attempts=0, last_error=None)
    result = await sched.arm(sched.store.alarm(alarm_id))
    refreshed = sched.store.alarm(alarm_id)
    return {
        "alarm": _decorate(refreshed, sched),
        "ok": result.ok,
        "message": result.message,
        "strap": monitor.as_dict(transport),
    }


@router.get("/api/alarms/status")
async def alarm_status() -> dict[str, Any]:
    return await get_scheduler().status()


@router.get("/api/strap/state")
def strap_state() -> dict[str, Any]:
    """Live connection state for the UI banner.

    Replaces the Phase 1 placeholder. The policy it reports is the design
    constraint from the brief: connect on demand, send, disconnect, never hold.
    """
    payload = monitor.as_dict(transport)
    payload["configured_address"] = settings.strap_address or None
    payload["bench_test"] = "docs/PHASE4_BENCH.md"
    if not transport.available:
        payload["hint"] = (
            "No strap configured. Run `python tools/bench_strap.py --scan`, then "
            "set STRAP_ADDRESS in .env and restart."
            if not settings.strap_address else
            "bleak is not installed. Run `pip install bleak` and restart."
        )
    return payload


@router.post("/api/strap/test")
async def strap_test(
    seconds: int = Query(20, ge=MIN_AHEAD_SECONDS, le=300,
                         description="Buzz this many seconds from now."),
) -> dict[str, Any]:
    """Set a throwaway alarm to confirm the strap actually buzzes.

    Wear the strap. This is the same path a real alarm takes, so a success here
    means the feature works end to end on your hardware.
    """
    sched = get_scheduler()
    fire_at = int(time.time()) + seconds
    alarm = sched.store.create_alarm(fire_at=fire_at, label="Connection test",
                                     kind="timer")
    result = await sched.arm(sched.store.alarm(alarm["id"]))
    refreshed = sched.store.alarm(alarm["id"])
    return {
        "ok": result.ok,
        "alarm": _decorate(refreshed, sched),
        "message": result.message,
        "expect_buzz_at": datetime.fromtimestamp(fire_at).strftime("%H:%M:%S"),
        "reminder": "Wear the strap — a haptic on a desk is easy to miss.",
        "strap": monitor.as_dict(transport),
    }
