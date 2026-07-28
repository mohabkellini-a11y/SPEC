"""The BLE frame format, as a test suite.

Phase 4 will build packets; this locks the framing down now, while the analysis
is fresh, against the captured packets in tools/verify_ble_frame.py.

Source of every frame: bWanShiTong/reverse-engineering-whoop-post @ 305e35d.
See BLE_NOTES.md for the derivation and for the correction to that writeup's
published CRC parameters.
"""

from __future__ import annotations

import struct
import sys
import zlib
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.verify_ble_frame import (  # noqa: E402
    CAPTURES,
    HEADER_ONLY,
    crc8_smbus,
    crc32_whoop_published,
)

ALL_FRAMES = [bytes.fromhex(h) for frames in CAPTURES.values() for h in frames]


@pytest.mark.parametrize("frame", ALL_FRAMES, ids=lambda f: f.hex()[:16])
def test_start_of_frame_is_aa(frame: bytes):
    assert frame[0] == 0xAA


@pytest.mark.parametrize("frame", ALL_FRAMES, ids=lambda f: f.hex()[:16])
def test_declared_length_matches_actual(frame: bytes):
    """total == len + 4. The 4 uncounted bytes are AA, len_lo, len_hi, hdr_crc."""
    declared = struct.unpack_from("<H", frame, 1)[0]
    assert len(frame) == declared + 4


@pytest.mark.parametrize("frame", ALL_FRAMES, ids=lambda f: f.hex()[:16])
def test_header_crc8_is_smbus_over_length(frame: bytes):
    assert frame[3] == crc8_smbus(frame[1:3])


@pytest.mark.parametrize("length,expected", HEADER_ONLY)
def test_header_crc8_for_longer_frames(length: int, expected: int):
    """Frames we only have the header of — 36/72/92 bytes."""
    assert crc8_smbus(struct.pack("<H", length)) == expected


@pytest.mark.parametrize("frame", ALL_FRAMES, ids=lambda f: f.hex()[:16])
def test_trailing_crc32_is_standard_over_payload(frame: bytes):
    """The correction: plain CRC-32 over frame[4:-4], little-endian."""
    want = struct.unpack_from("<I", frame, len(frame) - 4)[0]
    assert zlib.crc32(frame[4:-4]) == want


def test_published_crc_parameters_are_a_degenerate_fit():
    """Documents *why* BLE_NOTES.md 2.3 overrides the source writeup.

    The published params only reproduce frames sharing the prefix and length of
    the four alarm packets that were fed to crcbeagle.
    """
    ok = sum(
        1 for f in ALL_FRAMES
        if crc32_whoop_published(f[:-4]) == struct.unpack_from("<I", f, len(f) - 4)[0]
    )
    assert 0 < ok < len(ALL_FRAMES), "expected a partial match, not all-or-nothing"

    alarm = [bytes.fromhex(h) for h in CAPTURES["alarm set (0x42)"]]
    assert all(
        crc32_whoop_published(f[:-4]) == struct.unpack_from("<I", f, len(f) - 4)[0]
        for f in alarm
    ), "published params should still fit the frames they were fitted on"

    reboot = [bytes.fromhex(h) for h in CAPTURES["reboot (0x1d)"]]
    assert not any(
        crc32_whoop_published(f[:-4]) == struct.unpack_from("<I", f, len(f) - 4)[0]
        for f in reboot
    ), "published params should fail on a different prefix/length"


def test_failing_frames_differ_by_a_constant_xor():
    """The signature of a prefix absorbed into xor_out."""
    deltas = set()
    for frame in [bytes.fromhex(h) for h in CAPTURES["reboot (0x1d)"] + CAPTURES["activity start/stop (0x03)"]]:
        want = struct.unpack_from("<I", frame, len(frame) - 4)[0]
        deltas.add(crc32_whoop_published(frame[:-4]) ^ want)
    assert len(deltas) == 1, "all AA-08-00-A8 frames should be off by one constant"


# --- alarm payload ---------------------------------------------------------

ALARM_HEADER = bytes.fromhex("aa100057")
ALARM_TYPE = 0x23
ALARM_CMD = 0x42
ALARM_SUB = 0x01


@pytest.mark.parametrize("hexstr,expected_unix", [
    ("aa100057236d4201d036656600000000f62deb81", 0x666536D0),
    ("aa100057236e42010c376566000000001023ccef", 0x6665370C),
    ("aa100057236f4201207d656600000000fea1e060", 0x66657D20),
    ("aa100057237042015011656600000000f226a8bd", 0x66651150),
])
def test_alarm_frame_layout(hexstr: str, expected_unix: int):
    """AA 10 00 57 | 23 | ctr | 42 01 | u32 LE unix | 00000000 | crc32 LE."""
    frame = bytes.fromhex(hexstr)
    assert len(frame) == 20
    assert frame[:4] == ALARM_HEADER
    assert frame[4] == ALARM_TYPE
    assert frame[6] == ALARM_CMD
    assert frame[7] == ALARM_SUB
    assert struct.unpack_from("<I", frame, 8)[0] == expected_unix
    assert frame[12:16] == b"\x00\x00\x00\x00"
    assert zlib.crc32(frame[4:-4]) == struct.unpack_from("<I", frame, 16)[0]


def test_alarm_counter_increments_across_captures():
    """Byte 5 is a rolling counter. [RE] found the strap does not validate it."""
    counters = [bytes.fromhex(h)[5] for h in CAPTURES["alarm set (0x42)"][:4]]
    assert counters == [0x6D, 0x6E, 0x6F, 0x70]


def test_alarm_times_are_absolute_not_offsets():
    """Every captured alarm timestamp is a plausible absolute unix time.

    This is what makes a countdown timer trivial (now + N) and is worth pinning:
    if a future capture shows a small integer here, the layout changed.
    """
    for hexstr in CAPTURES["alarm set (0x42)"]:
        ts = struct.unpack_from("<I", bytes.fromhex(hexstr), 8)[0]
        assert 1_600_000_000 < ts < 2_000_000_000


# --- short command payload -------------------------------------------------

@pytest.mark.parametrize("hexstr,cmd,value", [
    ("aa0800a823070e00c7e40f08", 0x0E, 0x00),   # HR broadcast off
    ("aa0800a823080e016c935474", 0x0E, 0x01),   # HR broadcast on
    ("aa0800a8238c03017d5ec627", 0x03, 0x01),   # activity start
    ("aa0800a8238d0300dc040351", 0x03, 0x00),   # activity stop
    ("aa0800a823d41d003c2e2fe6", 0x1D, 0x00),   # reboot
])
def test_short_command_layout(hexstr: str, cmd: int, value: int):
    """AA 08 00 A8 | 23 | ctr | cmd | val | crc32 LE."""
    frame = bytes.fromhex(hexstr)
    assert len(frame) == 12
    assert frame[4] == 0x23
    assert frame[6] == cmd
    assert frame[7] == value
    assert zlib.crc32(frame[4:-4]) == struct.unpack_from("<I", frame, 8)[0]


def test_all_command_frames_use_type_0x23():
    """Everything written to CMD_TO_STRAP carries payload type 0x23."""
    command_groups = ["alarm set (0x42)", "hr broadcast toggle (0x0e)",
                      "activity start/stop (0x03)", "misc commands",
                      "reboot (0x1d)", "erase (0x19)"]
    for group in command_groups:
        for hexstr in CAPTURES[group]:
            assert bytes.fromhex(hexstr)[4] == 0x23, f"{group}: {hexstr}"
