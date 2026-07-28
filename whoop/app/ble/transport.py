"""Talking to the strap: a swappable transport behind one interface.

Two rules from the brief drive this whole module:

  * **Never hold an idle connection.** The strap bonds to one host at a time, so
    every send is connect → write → disconnect. There is no persistent client,
    no keep-alive, no reconnect loop.
  * **A failure must be legible.** "I need to know if an alarm did NOT get set."
    Every failure is classified into a `FailureKind` with a sentence a human can
    act on, rather than surfacing a bleak stack trace.

The transport is an interface because the bench test may say `bleak` cannot write
to this strap (BLE_NOTES.md §4.2 — the researcher reports exactly that). If so,
a `gatttool`/`bluetoothctl` backend slots in here and nothing above this file
changes: the packet builders, scheduler, API and UI are all transport-agnostic.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol

from . import packets as pk


class StrapState(str, Enum):
    """What the radio is doing right now. Rendered as a banner in the UI."""

    IDLE = "idle"                  # nothing connected — the resting state
    SCANNING = "scanning"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    WRITING = "writing"
    DISCONNECTING = "disconnecting"
    ERROR = "error"
    UNAVAILABLE = "unavailable"    # no transport at all (bleak missing, BLE off)


class FailureKind(str, Enum):
    """Why a send failed, in terms that suggest what to do about it."""

    NOT_CONFIGURED = "not_configured"
    NO_TRANSPORT = "no_transport"
    NOT_FOUND = "not_found"
    CLAIMED = "claimed"              # another app holds the bond
    NOT_BONDED = "not_bonded"        # connected, but no private service
    WRITE_REJECTED = "write_rejected"
    TIMEOUT = "timeout"
    UNKNOWN = "unknown"


#: Human-readable explanation per failure kind. These are shown verbatim in the
#: UI, so they are written for the person reading them at 6am, not for a log.
FAILURE_MESSAGE: dict[FailureKind, str] = {
    FailureKind.NOT_CONFIGURED: (
        "No strap address configured. Run `python tools/bench_strap.py --scan`, "
        "then put the address in STRAP_ADDRESS in your .env."
    ),
    FailureKind.NO_TRANSPORT: (
        "Bluetooth support is not installed. Run `pip install bleak` and restart "
        "the dashboard."
    ),
    FailureKind.NOT_FOUND: (
        "The strap did not answer. It may be out of range, asleep or on the "
        "charger. Put it on and try again."
    ),
    FailureKind.CLAIMED: (
        "Another app is holding the strap — usually the WHOOP app on your phone, "
        "or NOOP on this machine. A WHOOP strap pairs with one device at a time. "
        "Close the other app and try again."
    ),
    FailureKind.NOT_BONDED: (
        "Connected, but the strap is not paired to this machine, so it will not "
        "accept commands. Live heart rate still works without pairing, which is "
        "why this can look fine and still fail. See docs/PHASE4_BENCH.md step 3."
    ),
    FailureKind.WRITE_REJECTED: (
        "The strap refused the command. This is usually a pairing problem rather "
        "than a bad command — the packet format is verified against captures."
    ),
    FailureKind.TIMEOUT: (
        "Timed out talking to the strap. It may have moved out of range mid-send."
    ),
    FailureKind.UNKNOWN: "The send failed for a reason this app does not recognise.",
}


def classify(exc: BaseException) -> FailureKind:
    """Map a transport exception onto something actionable."""
    text = f"{type(exc).__name__}: {exc}".lower()
    if isinstance(exc, asyncio.TimeoutError) or "timeout" in text or "timed out" in text:
        return FailureKind.TIMEOUT
    if any(k in text for k in ("encryption", "insufficient", "bond", "not paired",
                               "authentication", "unauthorized")):
        return FailureKind.NOT_BONDED
    if any(k in text for k in ("in use", "busy", "already connected", "resource")):
        return FailureKind.CLAIMED
    if any(k in text for k in ("not found", "no device", "was not found", "unreachable")):
        return FailureKind.NOT_FOUND
    if any(k in text for k in ("write", "not permitted", "rejected", "gatt")):
        return FailureKind.WRITE_REJECTED
    return FailureKind.UNKNOWN


@dataclass
class SendResult:
    """Outcome of one connect-send-disconnect cycle."""

    ok: bool
    frame_hex: str
    kind: FailureKind | None = None
    detail: str | None = None
    replies: list[str] = field(default_factory=list)
    duration_s: float = 0.0
    at: float = field(default_factory=time.time)

    @property
    def message(self) -> str:
        if self.ok:
            return "Sent to the strap."
        base = FAILURE_MESSAGE.get(self.kind or FailureKind.UNKNOWN,
                                   FAILURE_MESSAGE[FailureKind.UNKNOWN])
        return f"{base} ({self.detail})" if self.detail else base

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "frame_hex": self.frame_hex,
            "kind": self.kind.value if self.kind else None,
            "message": self.message,
            "detail": self.detail,
            "replies": self.replies,
            "duration_s": round(self.duration_s, 2),
            "at": self.at,
        }


class Transport(Protocol):
    """What the scheduler needs. Deliberately tiny."""

    name: str

    async def send(self, frame: bytes, *, expect_reply: bool = False) -> SendResult: ...

    @property
    def available(self) -> bool: ...


class UnavailableTransport:
    """Stands in when BLE cannot be used at all.

    Everything above it keeps working — you can still schedule alarms, and they
    persist — but each send fails immediately with the reason. This is what makes
    the "alarm did NOT get set" path testable without hardware, and it is what the
    dashboard uses when `bleak` is missing or no address is configured.
    """

    name = "unavailable"

    def __init__(self, kind: FailureKind = FailureKind.NO_TRANSPORT,
                 detail: str | None = None):
        self.kind = kind
        self.detail = detail

    @property
    def available(self) -> bool:
        return False

    async def send(self, frame: bytes, *, expect_reply: bool = False) -> SendResult:
        return SendResult(ok=False, frame_hex=frame.hex(), kind=self.kind,
                          detail=self.detail)


class BleakTransport:
    """Real BLE over `bleak`. Connect, write, disconnect. Nothing held open.

    A single lock serialises access so two API calls can never race for the
    radio — the strap would refuse the second anyway, but the resulting error
    would be misleading.
    """

    name = "bleak"

    def __init__(self, address: str, *, connect_timeout: float = 20.0,
                 reply_window: float = 3.0, on_state=None):
        self.address = address
        self.connect_timeout = connect_timeout
        self.reply_window = reply_window
        self._lock = asyncio.Lock()
        self._on_state = on_state

    @property
    def available(self) -> bool:
        try:
            import bleak  # noqa: F401
        except ImportError:
            return False
        return bool(self.address)

    def _state(self, state: StrapState, detail: str | None = None) -> None:
        if self._on_state:
            self._on_state(state, detail)

    async def send(self, frame: bytes, *, expect_reply: bool = False) -> SendResult:
        started = time.time()
        if not self.address:
            return SendResult(ok=False, frame_hex=frame.hex(),
                              kind=FailureKind.NOT_CONFIGURED)
        try:
            from bleak import BleakClient
        except ImportError:
            return SendResult(ok=False, frame_hex=frame.hex(),
                              kind=FailureKind.NO_TRANSPORT)

        async with self._lock:
            client = BleakClient(self.address, timeout=self.connect_timeout)
            replies: list[str] = []
            connected = False
            try:
                self._state(StrapState.CONNECTING)
                await client.connect()
                connected = True
                self._state(StrapState.CONNECTED)

                chars = {c.uuid.lower()
                         for service in client.services
                         for c in service.characteristics}
                if pk.CMD_TO_STRAP.lower() not in chars:
                    # Connected but the private service is absent — the exact
                    # signature of a strap bonded to something else.
                    return SendResult(
                        ok=False, frame_hex=frame.hex(), kind=FailureKind.NOT_BONDED,
                        detail="CMD_TO_STRAP not exposed",
                        duration_s=time.time() - started)

                if expect_reply and pk.CMD_FROM_STRAP.lower() in chars:
                    def handler(_sender, data: bytearray) -> None:
                        replies.append(bytes(data).hex())
                    try:
                        await client.start_notify(pk.CMD_FROM_STRAP, handler)
                    except Exception:  # noqa: BLE001, S110 - replies are a bonus
                        pass

                self._state(StrapState.WRITING)
                props = {c.uuid.lower(): c.properties
                         for service in client.services
                         for c in service.characteristics}
                with_response = "write" in props.get(pk.CMD_TO_STRAP.lower(), [])
                await client.write_gatt_char(pk.CMD_TO_STRAP, frame,
                                             response=with_response)

                if expect_reply:
                    await asyncio.sleep(self.reply_window)

                return SendResult(ok=True, frame_hex=frame.hex(), replies=replies,
                                  duration_s=time.time() - started)

            except Exception as exc:  # noqa: BLE001 - classified, not swallowed
                kind = classify(exc)
                self._state(StrapState.ERROR, str(exc))
                return SendResult(ok=False, frame_hex=frame.hex(), kind=kind,
                                  detail=f"{type(exc).__name__}: {exc}",
                                  duration_s=time.time() - started)
            finally:
                # The rule: the radio is never left held, on any path.
                if connected:
                    self._state(StrapState.DISCONNECTING)
                    try:
                        await client.disconnect()
                    except Exception:  # noqa: BLE001, S110
                        pass
                self._state(StrapState.IDLE)


class StrapMonitor:
    """Tracks the last known state and the last send, for the UI banner."""

    def __init__(self) -> None:
        self.state: StrapState = StrapState.IDLE
        self.detail: str | None = None
        self.changed_at: float = time.time()
        self.last_result: SendResult | None = None

    def on_state(self, state: StrapState, detail: str | None = None) -> None:
        self.state = state
        self.detail = detail
        self.changed_at = time.time()

    def record(self, result: SendResult) -> None:
        self.last_result = result

    def as_dict(self, transport: Transport | None = None) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "detail": self.detail,
            "changed_at": self.changed_at,
            "transport": transport.name if transport else None,
            "available": bool(transport and transport.available),
            "connection_policy": (
                "Connects on demand, sends, and disconnects. No connection is ever "
                "held while idle — the strap pairs with one device at a time."
            ),
            "last_send": self.last_result.as_dict() if self.last_result else None,
        }


def build_transport(address: str | None, monitor: StrapMonitor) -> Transport:
    """Pick a transport from configuration, explaining any downgrade."""
    if not address:
        return UnavailableTransport(FailureKind.NOT_CONFIGURED)
    try:
        import bleak  # noqa: F401
    except ImportError:
        return UnavailableTransport(FailureKind.NO_TRANSPORT)
    return BleakTransport(address, on_state=monitor.on_state)
