"""Alarm scheduling.

Split in two on purpose:

  * `plan()` is a pure function — given the alarm rows and the current time, it
    decides what should happen. Fully testable without a radio, which matters
    because the radio is the part we cannot test here.
  * `AlarmScheduler` executes that plan against a `Transport`.

How alarms actually work on this hardware
-----------------------------------------
The strap stores an **absolute unix time** (BLE_NOTES.md §3.2). So once an alarm
is armed it fires on the strap's own clock — this machine can be asleep, off, or
out of range and the buzz still happens. That is the good news, and it is why
arming happens as soon as you create the alarm rather than at fire time.

The bad news is that **the strap holds one alarm at a time** — the source
writeup's opening complaint is that WHOOP's alarm "can only be set to ring only
once a day". So with several alarms scheduled, only the *earliest* is really on
the strap. The rest are queued here, and each is armed after the one before it
fires — which needs this dashboard to be running at that moment. The UI says
which alarms are truly on the strap and which are only queued, because the
difference decides whether you will actually be woken up.

Cancelling
----------
There is no verified un-set command (BLE_NOTES.md §3.3). Cancelling an alarm
that is already on the strap removes it from *our* schedule and says plainly
that the strap may still buzz. Arming a different alarm probably replaces it,
but "probably" is not something to promise about an alarm.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Sequence

from . import packets as pk
from .transport import SendResult, StrapMonitor, Transport

#: How often the runner re-evaluates. Cheap: it only touches the radio when the
#: plan says something must change.
TICK_SECONDS = 20.0

#: Arming attempts before an alarm is given up on and marked failed for good.
#: Surfaced in the UI rather than retried silently forever.
MAX_ATTEMPTS = 3

#: An alarm whose time passed while still pending never reached the strap.
#: Beyond this much lateness we stop trying and report it, rather than arming
#: something in the past.
LATE_GRACE_SECONDS = 30

ACTIVE_STATES = ("pending", "armed", "failed")


@dataclass
class Plan:
    """What should change on this pass."""

    to_arm: dict[str, Any] | None = None
    to_demote: list[dict[str, Any]] = field(default_factory=list)
    to_expire: list[tuple[dict[str, Any], str, str]] = field(default_factory=list)
    queued: list[dict[str, Any]] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (self.to_arm or self.to_demote or self.to_expire)


def plan(alarms: Sequence[dict[str, Any]], now: int) -> Plan:
    """Decide what to arm, demote and expire. Pure.

    `alarms` may contain rows in any state; only pending/armed/failed matter.
    """
    result = Plan()
    active = [a for a in alarms if a["state"] in ACTIVE_STATES]

    past = [a for a in active if int(a["fire_at"]) <= now - LATE_GRACE_SECONDS]
    for alarm in past:
        if alarm["state"] == "armed":
            # We cannot observe the buzz — the connection is long closed. The
            # honest label is "presumed", and the UI says so.
            result.to_expire.append(
                (alarm, "fired", "Fire time passed while armed on the strap (presumed fired)."))
        else:
            result.to_expire.append(
                (alarm, "failed",
                 "Its time passed before it could be set on the strap, so it did not buzz."))

    upcoming = sorted((a for a in active if a not in past), key=lambda a: int(a["fire_at"]))
    if not upcoming:
        return result

    nxt = upcoming[0]
    result.queued = upcoming[1:]

    if nxt["state"] != "armed":
        if int(nxt.get("attempts") or 0) < MAX_ATTEMPTS:
            result.to_arm = nxt
        # else: left as failed, already reported; do not retry forever.

    # The strap holds one alarm, so anything else marked armed is stale.
    for alarm in upcoming[1:]:
        if alarm["state"] == "armed":
            result.to_demote.append(alarm)

    return result


def describe_alarm(alarm: dict[str, Any], armed_id: int | None, now: int) -> dict[str, Any]:
    """Add the fields the UI needs to be honest about an alarm's real status."""
    fire_at = int(alarm["fire_at"])
    on_strap = alarm["state"] == "armed" and alarm["id"] == armed_id
    seconds_away = fire_at - now

    if alarm["state"] == "cancelled":
        status = "Cancelled"
    elif alarm["state"] == "fired":
        status = "Fired (presumed)"
    elif alarm["state"] == "failed":
        status = "NOT SET — will not buzz"
    elif on_strap:
        status = "On the strap"
    else:
        status = "Queued — not on the strap yet"

    return {
        **alarm,
        "on_strap": on_strap,
        "seconds_away": seconds_away,
        "status": status,
        "will_survive_shutdown": on_strap,
        "note": (
            "Armed on the strap: it will buzz on the strap's own clock even if "
            "this machine is off."
            if on_strap else
            "Queued here. The strap holds one alarm at a time, so this is armed "
            "only after the one before it fires — which needs the dashboard running."
            if alarm["state"] in ("pending", "failed") and seconds_away > 0 else ""
        ),
    }


class AlarmScheduler:
    """Runs the plan against the strap. One instance per process."""

    def __init__(self, store, transport: Transport, monitor: StrapMonitor,
                 tick_seconds: float = TICK_SECONDS):
        self.store = store
        self.transport = transport
        self.monitor = monitor
        self.tick_seconds = tick_seconds
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._counter = pk.Counter(start=0x70)
        self.last_pass_at: float | None = None
        self.last_error: str | None = None

    # -- lifecycle -------------------------------------------------------

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop(), name="alarm-scheduler")

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None

    def wake(self) -> None:
        """Ask for an immediate pass — called after any change to the schedule."""
        self._wake.set()

    @property
    def running(self) -> bool:
        return bool(self._task and not self._task.done())

    async def _loop(self) -> None:
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - a bad pass must not kill the loop
                self.last_error = f"{type(exc).__name__}: {exc}"
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.tick_seconds)
            except asyncio.TimeoutError:
                pass
            finally:
                self._wake.clear()

    # -- one pass --------------------------------------------------------

    def armed_id(self) -> int | None:
        """Which alarm we believe is actually on the strap."""
        armed = self.store.alarms(states=("armed",))
        return int(armed[0]["id"]) if armed else None

    async def run_once(self, now: int | None = None) -> Plan:
        now = int(now if now is not None else time.time())
        current = plan(self.store.alarms(states=ACTIVE_STATES), now)

        for alarm, state, reason in current.to_expire:
            self.store.update_alarm(alarm["id"], state=state, last_error=reason)

        for alarm in current.to_demote:
            self.store.update_alarm(
                alarm["id"], state="pending",
                last_error="Displaced: the strap holds one alarm and an earlier one took it.")

        if current.to_arm is not None:
            await self.arm(current.to_arm, now)

        self.last_pass_at = time.time()
        return current

    async def arm(self, alarm: dict[str, Any], now: int | None = None) -> SendResult:
        """Push one alarm to the strap and record exactly what happened."""
        now = int(now if now is not None else time.time())
        fire_at = int(alarm["fire_at"])
        frame = pk.alarm_set(fire_at, self._counter.next())

        attempts = self.store.bump_alarm_attempt(alarm["id"])
        result = await self.transport.send(frame)
        self.monitor.record(result)

        if result.ok:
            self.store.update_alarm(
                alarm["id"], state="armed", frame_hex=result.frame_hex,
                armed_at=str(int(time.time())), last_error=None)
        else:
            give_up = attempts >= MAX_ATTEMPTS
            self.store.update_alarm(
                alarm["id"],
                state="failed" if give_up else "pending",
                frame_hex=result.frame_hex,
                last_error=(
                    f"{result.message}"
                    + ("" if give_up else f" Retrying (attempt {attempts} of {MAX_ATTEMPTS}).")
                ),
            )
        return result

    # -- user actions ----------------------------------------------------

    async def cancel(self, alarm_id: int) -> dict[str, Any]:
        """Cancel an alarm, and be honest about what that does and does not do."""
        alarm = self.store.alarm(alarm_id)
        was_armed = alarm["state"] == "armed"
        self.store.update_alarm(alarm_id, state="cancelled", last_error=None)

        warning = None
        if was_armed:
            warning = (
                "Removed from your schedule, but this alarm was already set on the "
                "strap and there is no verified command to un-set it. The strap may "
                "still buzz at the original time. Setting another alarm probably "
                "replaces it, but that is not verified — see BLE_NOTES.md 3.3."
            )

        self.wake()
        return {
            "cancelled": True,
            "alarm_id": alarm_id,
            "was_on_strap": was_armed,
            "strap_may_still_fire": was_armed,
            "warning": warning,
        }

    async def status(self) -> dict[str, Any]:
        now = int(time.time())
        rows = self.store.alarms(states=ACTIVE_STATES)
        armed = self.armed_id()
        return {
            "running": self.running,
            "tick_seconds": self.tick_seconds,
            "last_pass_at": self.last_pass_at,
            "last_error": self.last_error,
            "armed_alarm_id": armed,
            "pending_count": len(rows),
            "strap": self.monitor.as_dict(self.transport),
            "next": (describe_alarm(sorted(rows, key=lambda a: a["fire_at"])[0], armed, now)
                     if rows else None),
            "one_alarm_limit": (
                "The strap holds one alarm at a time. Only the next one is on the "
                "strap; the rest are armed in turn as each fires."
            ),
        }
