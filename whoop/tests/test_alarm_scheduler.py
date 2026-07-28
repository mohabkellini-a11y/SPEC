"""Scheduler tests.

`plan()` is pure, so the awkward parts — a strap that holds one alarm, an alarm
whose time passed while we were shut down, a write that fails — are all testable
without a radio. That is most of why the module is split this way.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.ble.scheduler import (  # noqa: E402
    LATE_GRACE_SECONDS,
    MAX_ATTEMPTS,
    AlarmScheduler,
    describe_alarm,
    plan,
)
from app.ble.transport import (  # noqa: E402
    FailureKind,
    SendResult,
    StrapMonitor,
    UnavailableTransport,
    classify,
)
from app.store import AppStore  # noqa: E402

NOW = 1_800_000_000


def alarm(id_: int, fire_at: int, state: str = "pending", attempts: int = 0) -> dict:
    return {"id": id_, "fire_at": fire_at, "state": state, "attempts": attempts,
            "label": f"a{id_}", "kind": "alarm"}


# --- plan(): what should be on the strap -----------------------------------


def test_earliest_pending_alarm_is_armed():
    p = plan([alarm(1, NOW + 7200), alarm(2, NOW + 600)], NOW)
    assert p.to_arm["id"] == 2
    assert [a["id"] for a in p.queued] == [1]


def test_nothing_to_do_when_the_next_alarm_is_already_armed():
    p = plan([alarm(1, NOW + 600, state="armed"), alarm(2, NOW + 7200)], NOW)
    assert p.to_arm is None
    assert p.to_demote == []


def test_a_newly_added_earlier_alarm_takes_the_strap():
    """The strap holds one alarm, so an earlier one must displace the armed one."""
    p = plan([alarm(1, NOW + 7200, state="armed"), alarm(2, NOW + 600)], NOW)
    assert p.to_arm["id"] == 2
    assert [a["id"] for a in p.to_demote] == [1]


def test_demoted_alarms_are_not_lost():
    p = plan([alarm(1, NOW + 7200, state="armed"), alarm(2, NOW + 600)], NOW)
    assert [a["id"] for a in p.queued] == [1]


def test_armed_alarm_whose_time_passed_is_presumed_fired():
    p = plan([alarm(1, NOW - 600, state="armed")], NOW)
    assert len(p.to_expire) == 1
    _row, state, reason = p.to_expire[0]
    assert state == "fired"
    assert "presumed" in reason.lower()


def test_pending_alarm_whose_time_passed_is_a_failure_not_a_fire():
    """It never reached the strap, so it did not buzz. Say so."""
    p = plan([alarm(1, NOW - 600, state="pending")], NOW)
    _row, state, reason = p.to_expire[0]
    assert state == "failed"
    assert "did not buzz" in reason


def test_an_alarm_within_the_grace_window_is_not_expired_yet():
    p = plan([alarm(1, NOW - LATE_GRACE_SECONDS + 5, state="armed")], NOW)
    assert p.to_expire == []


def test_exhausted_attempts_are_not_retried_forever():
    p = plan([alarm(1, NOW + 600, state="failed", attempts=MAX_ATTEMPTS)], NOW)
    assert p.to_arm is None


def test_a_failed_alarm_with_attempts_left_is_retried():
    p = plan([alarm(1, NOW + 600, state="failed", attempts=1)], NOW)
    assert p.to_arm["id"] == 1


def test_cancelled_and_fired_alarms_are_ignored():
    p = plan([alarm(1, NOW + 600, state="cancelled"),
              alarm(2, NOW + 700, state="fired")], NOW)
    assert p.empty
    assert p.to_arm is None


def test_empty_schedule_is_an_empty_plan():
    assert plan([], NOW).empty


# --- describe_alarm(): what the UI is told ---------------------------------


def test_only_the_armed_alarm_claims_to_be_on_the_strap():
    armed = describe_alarm(alarm(1, NOW + 600, state="armed"), armed_id=1, now=NOW)
    queued = describe_alarm(alarm(2, NOW + 900, state="pending"), armed_id=1, now=NOW)
    assert armed["on_strap"] is True
    assert armed["status"] == "On the strap"
    assert armed["will_survive_shutdown"] is True

    assert queued["on_strap"] is False
    assert "not on the strap" in queued["status"].lower()
    assert queued["will_survive_shutdown"] is False


def test_an_armed_row_that_is_not_the_armed_id_does_not_claim_the_strap():
    """Guards against a stale 'armed' row lying to the user."""
    stale = describe_alarm(alarm(9, NOW + 600, state="armed"), armed_id=1, now=NOW)
    assert stale["on_strap"] is False


def test_failed_alarm_says_it_will_not_buzz():
    failed = describe_alarm(alarm(1, NOW + 600, state="failed"), armed_id=None, now=NOW)
    assert "NOT SET" in failed["status"]


# --- transport classification ----------------------------------------------


@pytest.mark.parametrize("message,expected", [
    ("Encryption is insufficient", FailureKind.NOT_BONDED),
    ("bond refused by peer", FailureKind.NOT_BONDED),
    ("Device is already connected", FailureKind.CLAIMED),
    ("Device with address AA was not found", FailureKind.NOT_FOUND),
    ("GATT write not permitted", FailureKind.WRITE_REJECTED),
    ("something nobody has seen", FailureKind.UNKNOWN),
])
def test_failures_are_classified_into_actionable_kinds(message, expected):
    assert classify(RuntimeError(message)) == expected


def test_timeout_is_classified():
    import asyncio
    assert classify(asyncio.TimeoutError()) == FailureKind.TIMEOUT


def test_every_failure_kind_has_a_human_message():
    for kind in FailureKind:
        result = SendResult(ok=False, frame_hex="00", kind=kind)
        assert len(result.message) > 30
        assert "Traceback" not in result.message


def test_a_successful_result_says_so():
    assert SendResult(ok=True, frame_hex="00").message == "Sent to the strap."


@pytest.mark.asyncio
async def test_unavailable_transport_fails_with_a_reason():
    transport = UnavailableTransport(FailureKind.NOT_CONFIGURED)
    result = await transport.send(b"\xaa\x08\x00\xa8data")
    assert result.ok is False
    assert "bench_strap" in result.message
    assert transport.available is False


# --- scheduler against fake transports -------------------------------------


class FakeTransport:
    """Records what it was asked to send, and can be told to fail."""

    name = "fake"

    def __init__(self, ok: bool = True, kind: FailureKind | None = None):
        self.ok = ok
        self.kind = kind
        self.sent: list[bytes] = []

    @property
    def available(self) -> bool:
        return True

    async def send(self, frame: bytes, *, expect_reply: bool = False) -> SendResult:
        self.sent.append(frame)
        return SendResult(ok=self.ok, frame_hex=frame.hex(),
                          kind=None if self.ok else (self.kind or FailureKind.UNKNOWN))


@pytest.fixture
def store(tmp_path: Path) -> AppStore:
    s = AppStore(tmp_path / "app.sqlite3")
    s.ensure_ready()
    return s


def make(store: AppStore, transport) -> AlarmScheduler:
    return AlarmScheduler(store, transport, StrapMonitor())


@pytest.mark.asyncio
async def test_a_successful_arm_marks_the_alarm_armed(store: AppStore):
    transport = FakeTransport(ok=True)
    sched = make(store, transport)
    created = store.create_alarm(fire_at=int(time.time()) + 600, label="Wake")

    await sched.run_once()

    row = store.alarm(created["id"])
    assert row["state"] == "armed"
    assert row["frame_hex"]
    assert row["armed_at"]
    assert len(transport.sent) == 1
    assert sched.armed_id() == created["id"]


@pytest.mark.asyncio
async def test_the_frame_sent_is_a_real_alarm_frame(store: AppStore):
    from app.ble import packets as pk
    transport = FakeTransport(ok=True)
    sched = make(store, transport)
    fire_at = int(time.time()) + 600
    store.create_alarm(fire_at=fire_at)

    await sched.run_once()

    decoded = pk.decode_frame(transport.sent[0])
    assert decoded.command == pk.CMD_ALARM_SET
    import struct
    assert struct.unpack_from("<I", decoded.payload, 4)[0] == fire_at


@pytest.mark.asyncio
async def test_a_failed_arm_is_recorded_with_a_readable_reason(store: AppStore):
    sched = make(store, FakeTransport(ok=False, kind=FailureKind.CLAIMED))
    created = store.create_alarm(fire_at=int(time.time()) + 600)

    await sched.run_once()

    row = store.alarm(created["id"])
    assert row["state"] == "pending"           # retried, not abandoned yet
    assert row["attempts"] == 1
    assert "Another app is holding the strap" in row["last_error"]
    assert "Retrying" in row["last_error"]


@pytest.mark.asyncio
async def test_repeated_failures_give_up_and_say_so(store: AppStore):
    sched = make(store, FakeTransport(ok=False, kind=FailureKind.NOT_BONDED))
    created = store.create_alarm(fire_at=int(time.time()) + 600)

    for _ in range(MAX_ATTEMPTS):
        await sched.run_once()

    row = store.alarm(created["id"])
    assert row["state"] == "failed"
    assert row["attempts"] == MAX_ATTEMPTS
    assert "Retrying" not in row["last_error"]

    # And it stops trying rather than hammering the radio forever.
    before = len(sched.transport.sent)
    await sched.run_once()
    assert len(sched.transport.sent) == before


@pytest.mark.asyncio
async def test_only_one_alarm_is_ever_on_the_strap(store: AppStore):
    transport = FakeTransport(ok=True)
    sched = make(store, transport)
    later = store.create_alarm(fire_at=int(time.time()) + 7200, label="later")
    await sched.run_once()
    assert store.alarm(later["id"])["state"] == "armed"

    earlier = store.create_alarm(fire_at=int(time.time()) + 600, label="earlier")
    await sched.run_once()

    assert store.alarm(earlier["id"])["state"] == "armed"
    assert store.alarm(later["id"])["state"] == "pending"
    assert "Displaced" in store.alarm(later["id"])["last_error"]
    assert sched.armed_id() == earlier["id"]


@pytest.mark.asyncio
async def test_restart_reconciles_an_alarm_that_fired_while_we_were_down(store: AppStore):
    """The point of persisting: coming back up must not silently re-arm the past."""
    sched = make(store, FakeTransport(ok=True))
    past = store.create_alarm(fire_at=int(time.time()) - 3600)
    store.update_alarm(past["id"], state="armed")
    future = store.create_alarm(fire_at=int(time.time()) + 600)

    await sched.run_once()

    assert store.alarm(past["id"])["state"] == "fired"
    assert store.alarm(future["id"])["state"] == "armed"


@pytest.mark.asyncio
async def test_an_alarm_missed_while_down_is_reported_as_failed(store: AppStore):
    sched = make(store, FakeTransport(ok=True))
    missed = store.create_alarm(fire_at=int(time.time()) - 3600)   # never armed

    await sched.run_once()

    row = store.alarm(missed["id"])
    assert row["state"] == "failed"
    assert "did not buzz" in row["last_error"]


# --- cancelling -------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancelling_a_queued_alarm_is_clean(store: AppStore):
    sched = make(store, FakeTransport(ok=True))
    created = store.create_alarm(fire_at=int(time.time()) + 7200)

    outcome = await sched.cancel(created["id"])

    assert outcome["cancelled"] is True
    assert outcome["was_on_strap"] is False
    assert outcome["strap_may_still_fire"] is False
    assert outcome["warning"] is None
    assert store.alarm(created["id"])["state"] == "cancelled"


@pytest.mark.asyncio
async def test_cancelling_an_armed_alarm_admits_it_may_still_buzz(store: AppStore):
    """There is no verified un-set command, so this must not claim success."""
    sched = make(store, FakeTransport(ok=True))
    created = store.create_alarm(fire_at=int(time.time()) + 7200)
    await sched.run_once()
    assert store.alarm(created["id"])["state"] == "armed"

    outcome = await sched.cancel(created["id"])

    assert outcome["was_on_strap"] is True
    assert outcome["strap_may_still_fire"] is True
    assert "may still buzz" in outcome["warning"]
    assert "not verified" in outcome["warning"]


@pytest.mark.asyncio
async def test_cancelling_frees_the_strap_for_the_next_alarm(store: AppStore):
    transport = FakeTransport(ok=True)
    sched = make(store, transport)
    first = store.create_alarm(fire_at=int(time.time()) + 600)
    second = store.create_alarm(fire_at=int(time.time()) + 7200)
    await sched.run_once()

    await sched.cancel(first["id"])
    await sched.run_once()

    assert store.alarm(second["id"])["state"] == "armed"


# --- status -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_status_reports_the_one_alarm_limit(store: AppStore):
    sched = make(store, FakeTransport(ok=True))
    store.create_alarm(fire_at=int(time.time()) + 600, label="soon")
    await sched.run_once()

    status = await sched.status()
    assert status["armed_alarm_id"]
    assert status["next"]["label"] == "soon"
    assert "one alarm at a time" in status["one_alarm_limit"]
    assert status["strap"]["connection_policy"].startswith("Connects on demand")


@pytest.mark.asyncio
async def test_scheduler_survives_a_transport_that_explodes(store: AppStore):
    class Exploding:
        name = "boom"
        available = True

        async def send(self, frame, *, expect_reply=False):
            raise RuntimeError("radio on fire")

    sched = make(store, Exploding())
    store.create_alarm(fire_at=int(time.time()) + 600)
    with pytest.raises(RuntimeError):
        await sched.run_once()
    # The loop catches it; run_once itself is allowed to propagate.
