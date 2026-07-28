"""WHOOP BLE frame codec — pure functions, no I/O, no bleak import.

The frame format is derived and verified in BLE_NOTES.md §2 against 39 captured
packets. Nothing in this module talks to a radio, so it is fully unit-testable
and stays correct regardless of whether the transport works on your machine.

    AA | len u16 LE | crc8(len) | payload | crc32 LE
                                  ^^^^^^^ len - 4 bytes
    crc8  = CRC-8/SMBUS  (poly 0x07, init 0x00, no reflect, xorout 0x00)
    crc32 = standard CRC-32 (zlib), little-endian, over frame[4:-4]

Where a command could not be verified from a source capture it raises
NotImplementedError rather than guessing at bytes. See `UNVERIFIED` below.
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass
from typing import Final

# --- GATT identifiers (BLE_NOTES.md §1) ------------------------------------

BASE_UUID_SUFFIX: Final = "-8d6d-82b8-614a-1c8cb0f8dcc6"

CMD_TO_STRAP: Final = f"61080002{BASE_UUID_SUFFIX}"
CMD_FROM_STRAP: Final = f"61080003{BASE_UUID_SUFFIX}"
EVENTS_FROM_STRAP: Final = f"61080004{BASE_UUID_SUFFIX}"
DATA_FROM_STRAP: Final = f"61080005{BASE_UUID_SUFFIX}"
MEMFAULT: Final = f"61080007{BASE_UUID_SUFFIX}"

#: Inferred from the characteristic numbering, not stated in any source.
SERVICE_UUID_GUESS: Final = f"61080001{BASE_UUID_SUFFIX}"

#: Standard Bluetooth SIG profiles the strap also exposes.
HEART_RATE_SERVICE: Final = "0000180d-0000-1000-8000-00805f9b34fb"
HEART_RATE_MEASUREMENT: Final = "00002a37-0000-1000-8000-00805f9b34fb"
BATTERY_SERVICE: Final = "0000180f-0000-1000-8000-00805f9b34fb"
BATTERY_LEVEL: Final = "00002a19-0000-1000-8000-00805f9b34fb"
DEVICE_INFO_SERVICE: Final = "0000180a-0000-1000-8000-00805f9b34fb"

# --- frame constants --------------------------------------------------------

SOF: Final = 0xAA
HEADER_LEN: Final = 4          # AA, len_lo, len_hi, crc8
TRAILER_LEN: Final = 4         # crc32
MAX_PAYLOAD: Final = 0xFFFF - TRAILER_LEN

TYPE_COMMAND: Final = 0x23     # payload byte 0 for everything we send

# Command bytes, all observed in captures (BLE_NOTES.md §3.1)
CMD_ACTIVITY: Final = 0x03     # start/stop activity + recording
CMD_HR_BROADCAST: Final = 0x0E # standard-profile HR broadcast on/off
CMD_DATA_RETRIEVE: Final = 0x16  # trigger a burst on DATA_FROM_STRAP
CMD_SYNC_BATCH: Final = 0x17   # request a history batch
CMD_ERASE: Final = 0x19
CMD_REBOOT: Final = 0x1D
CMD_ALARM_SET: Final = 0x42
CMD_ALARM_UNKNOWN_45: Final = 0x45  # [RE] labels "Alarm off"; see UNVERIFIED
CMD_DEVICE_NAME: Final = 0x4C
CMD_ENABLE_73: Final = 0x73
CMD_ENABLE_74: Final = 0x74

ALARM_SUBCOMMAND: Final = 0x01

UNVERIFIED: Final = {
    "alarm_cancel": (
        "No capture shows an alarm being cancelled. [RE] labels command 0x45 "
        "'Alarm off' but its value byte is 0x01 and 0x45 is listed as unknown "
        "elsewhere in the same document. Candidates are 0x45, 0x42 with "
        "subcommand 0x00, or an alarm set to a sentinel time. Guessing risks "
        "triggering something else entirely."
    ),
    "haptic_buzz": (
        "No capture shows a direct haptic command. NOOP's Breathe and Intervals "
        "features prove one exists, but NOOP's BLE layer is unpublished. Use "
        "alarm_set(now + 2) as a workaround — it buzzes, but it is an alarm, not "
        "a buzz primitive."
    ),
    "alarm_readback": (
        "Nothing is known about reading the currently-set alarm off the strap, "
        "so the backend's schedule is the only record of what is pending."
    ),
}


class FrameError(ValueError):
    """A frame failed to parse or failed a checksum."""


# --- checksums --------------------------------------------------------------


def crc8_smbus(data: bytes) -> int:
    """CRC-8/SMBUS: poly 0x07, init 0x00, no reflection, xorout 0x00."""
    crc = 0x00
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = ((crc << 1) ^ 0x07) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


def crc32_payload(payload: bytes) -> int:
    """Standard CRC-32 over the payload. See BLE_NOTES.md §2.3 for why this is
    plain zlib CRC-32 and not the parameters published by the source writeup."""
    return zlib.crc32(payload) & 0xFFFFFFFF


# --- frame encode / decode --------------------------------------------------


@dataclass(frozen=True)
class Frame:
    """A decoded frame. `payload` excludes both header and trailing CRC."""

    payload: bytes
    declared_len: int

    @property
    def type(self) -> int | None:
        return self.payload[0] if self.payload else None

    @property
    def counter(self) -> int | None:
        return self.payload[1] if len(self.payload) > 1 else None

    @property
    def command(self) -> int | None:
        return self.payload[2] if len(self.payload) > 2 else None

    @property
    def value(self) -> int | None:
        return self.payload[3] if len(self.payload) > 3 else None


def encode_frame(payload: bytes) -> bytes:
    """Wrap a payload in the header and trailing CRC-32."""
    if not payload:
        raise FrameError("payload must not be empty")
    if len(payload) > MAX_PAYLOAD:
        raise FrameError(f"payload too long: {len(payload)} > {MAX_PAYLOAD}")

    declared = len(payload) + TRAILER_LEN
    length_bytes = struct.pack("<H", declared)
    header = bytes([SOF]) + length_bytes + bytes([crc8_smbus(length_bytes)])
    return header + payload + struct.pack("<I", crc32_payload(payload))


def decode_frame(frame: bytes, *, verify: bool = True) -> Frame:
    """Parse a frame, checking both checksums.

    `verify=False` parses structurally without enforcing the CRCs — useful when
    logging a malformed notification rather than discarding it.
    """
    if len(frame) < HEADER_LEN + TRAILER_LEN + 1:
        raise FrameError(f"frame too short: {len(frame)} bytes")
    if frame[0] != SOF:
        raise FrameError(f"bad start byte: 0x{frame[0]:02x}, expected 0xAA")

    declared = struct.unpack_from("<H", frame, 1)[0]
    if verify:
        expected_crc8 = crc8_smbus(frame[1:3])
        if frame[3] != expected_crc8:
            raise FrameError(
                f"header CRC-8 mismatch: got 0x{frame[3]:02x}, expected 0x{expected_crc8:02x}"
            )
        if len(frame) != declared + HEADER_LEN:
            raise FrameError(
                f"length mismatch: header declares {declared} "
                f"(total {declared + HEADER_LEN}), got {len(frame)}"
            )

    payload = frame[HEADER_LEN:-TRAILER_LEN]
    if verify:
        want = struct.unpack_from("<I", frame, len(frame) - TRAILER_LEN)[0]
        got = crc32_payload(payload)
        if got != want:
            raise FrameError(f"payload CRC-32 mismatch: got 0x{got:08x}, expected 0x{want:08x}")

    return Frame(payload=payload, declared_len=declared)


# --- command builders -------------------------------------------------------


class Counter:
    """The rolling counter in payload byte 1.

    [RE] established the strap does **not** validate it — a replayed frame with a
    stale counter was accepted. We still increment so captures of our own traffic
    read sensibly next to the official app's.
    """

    def __init__(self, start: int = 0):
        self._value = start & 0xFF

    def next(self) -> int:
        value = self._value
        self._value = (self._value + 1) & 0xFF
        return value

    @property
    def value(self) -> int:
        return self._value


def short_command(command: int, value: int, counter: int) -> bytes:
    """12-byte command frame: AA 08 00 A8 | 23 ctr cmd val | crc32."""
    for name, byte in (("command", command), ("value", value), ("counter", counter)):
        if not 0 <= byte <= 0xFF:
            raise FrameError(f"{name} must be a single byte, got {byte}")
    return encode_frame(bytes([TYPE_COMMAND, counter, command, value]))


def alarm_set(when_unix: int, counter: int) -> bytes:
    """20-byte set-alarm frame (BLE_NOTES.md §3.2).

        AA 10 00 57 | 23 ctr 42 01 | <u32 LE unix> | 00 00 00 00 | crc32

    `when_unix` is an ABSOLUTE wall-clock time, which is what makes a countdown
    timer trivial: pass `now + seconds`.

    The four zero bytes are constant in every capture and their meaning is
    unknown; we send zeros because that is all that has ever been observed.
    """
    if not 0 <= when_unix <= 0xFFFFFFFF:
        raise FrameError(f"alarm time out of u32 range: {when_unix}")
    if not 0 <= counter <= 0xFF:
        raise FrameError(f"counter must be a single byte, got {counter}")

    payload = (
        bytes([TYPE_COMMAND, counter, CMD_ALARM_SET, ALARM_SUBCOMMAND])
        + struct.pack("<I", when_unix)
        + b"\x00\x00\x00\x00"
    )
    return encode_frame(payload)


def hr_broadcast(enabled: bool, counter: int) -> bytes:
    """Toggle the standard-profile heart-rate broadcast."""
    return short_command(CMD_HR_BROADCAST, 0x01 if enabled else 0x00, counter)


def trigger_data_retrieval(counter: int) -> bytes:
    """Ask the strap to burst history on DATA_FROM_STRAP.

    Used by the bench script as a low-risk write probe: it changes no persistent
    setting, and a notification arriving afterwards proves writes are landing.
    """
    return short_command(CMD_DATA_RETRIEVE, 0x00, counter)


def request_device_name(counter: int) -> bytes:
    """Command 0x4c, documented by [RE] as 'get the device name'.

    INFERRED: the command byte is documented but no full frame was captured, so
    the value byte (0x00) is an assumption from the shape of every other short
    command. Harmless if wrong — it is a read, not a setting.
    """
    return short_command(CMD_DEVICE_NAME, 0x00, counter)


def alarm_cancel(counter: int) -> bytes:  # noqa: ARG001 - signature is the contract
    """NOT IMPLEMENTED — see UNVERIFIED['alarm_cancel']."""
    raise NotImplementedError(UNVERIFIED["alarm_cancel"])


def haptic_buzz(counter: int) -> bytes:  # noqa: ARG001
    """NOT IMPLEMENTED — see UNVERIFIED['haptic_buzz']."""
    raise NotImplementedError(UNVERIFIED["haptic_buzz"])


# --- notification decoding --------------------------------------------------

PACKET_TYPES: Final = {
    0x23: "command",
    0x28: "live-hr-record",
    0x2F: "history-sample",
    0x30: "event",
    0x31: "sync-batch",
}


def describe(frame: bytes) -> str:
    """One-line human description of a frame, for logs and the bench report."""
    try:
        decoded = decode_frame(frame)
    except FrameError as exc:
        return f"{len(frame)}B undecodable ({exc}): {frame.hex()}"
    kind = PACKET_TYPES.get(decoded.type or -1, f"type 0x{decoded.type:02x}")
    bits = [f"{len(frame)}B", kind]
    if decoded.counter is not None:
        bits.append(f"ctr={decoded.counter}")
    if decoded.command is not None:
        bits.append(f"cmd=0x{decoded.command:02x}")
    if decoded.value is not None:
        bits.append(f"val=0x{decoded.value:02x}")
    return " ".join(bits) + f"  {frame.hex()}"


def parse_live_hr(frame: bytes) -> dict[str, int] | None:
    """Decode a 0x28 live record: unix seconds and bpm (BLE_NOTES.md §3).

    Layout from [RE]'s captures:
        23 ... | 28 02 <u32 LE unix> <2B ?> <bpm> <rr count> <10B rr data>
    Returns None for anything that is not a 0x28 record.
    """
    try:
        decoded = decode_frame(frame)
    except FrameError:
        return None
    payload = decoded.payload
    if not payload or payload[0] != 0x28 or len(payload) < 11:
        return None
    ts = struct.unpack_from("<I", payload, 2)[0]
    return {"ts": ts, "bpm": payload[8]}
