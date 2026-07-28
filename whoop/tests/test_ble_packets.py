"""Packet-builder tests — every builder must reproduce a captured frame byte for byte.

This is the part of Phase 4 that can be proven correct without hardware. If the
bench script fails on your strap, these tests say the bytes were not the reason.
"""

from __future__ import annotations

import struct
import sys
import zlib
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.ble import packets as pk  # noqa: E402
from tools.verify_ble_frame import CAPTURES  # noqa: E402


# --- checksums --------------------------------------------------------------


def test_crc8_matches_every_observed_header():
    for length, expected in [(8, 0xA8), (16, 0x57), (24, 0xFF), (28, 0xAB),
                             (36, 0xFA), (72, 0xF3), (92, 0xF0)]:
        assert pk.crc8_smbus(struct.pack("<H", length)) == expected


def test_crc8_of_known_vector():
    """CRC-8/SMBUS check value: "123456789" -> 0xF4."""
    assert pk.crc8_smbus(b"123456789") == 0xF4


def test_crc32_is_plain_zlib():
    assert pk.crc32_payload(b"123456789") == zlib.crc32(b"123456789")


# --- round trip -------------------------------------------------------------


@pytest.mark.parametrize(
    "hexstr",
    [h for frames in CAPTURES.values() for h in frames],
    ids=lambda h: h[:16],
)
def test_every_captured_frame_decodes(hexstr: str):
    frame = bytes.fromhex(hexstr)
    decoded = pk.decode_frame(frame)
    assert decoded.declared_len == len(frame) - 4
    assert len(decoded.payload) == len(frame) - 8


@pytest.mark.parametrize(
    "hexstr",
    [h for frames in CAPTURES.values() for h in frames],
    ids=lambda h: h[:16],
)
def test_re_encoding_a_captured_payload_reproduces_it_exactly(hexstr: str):
    """The strongest available check: decode a real frame, re-encode its payload,
    and require the bytes back."""
    frame = bytes.fromhex(hexstr)
    assert pk.encode_frame(pk.decode_frame(frame).payload) == frame


# --- builders vs captures ---------------------------------------------------


@pytest.mark.parametrize("hexstr,when,counter", [
    ("aa100057236d4201d036656600000000f62deb81", 0x666536D0, 0x6D),
    ("aa100057236e42010c376566000000001023ccef", 0x6665370C, 0x6E),
    ("aa100057236f4201207d656600000000fea1e060", 0x66657D20, 0x6F),
    ("aa100057237042015011656600000000f226a8bd", 0x66651150, 0x70),
])
def test_alarm_set_reproduces_captured_frames(hexstr: str, when: int, counter: int):
    assert pk.alarm_set(when, counter).hex() == hexstr


@pytest.mark.parametrize("hexstr,enabled,counter", [
    ("aa0800a823070e00c7e40f08", False, 0x07),
    ("aa0800a823080e016c935474", True, 0x08),
    ("aa0800a823090e00cdc99102", False, 0x09),
])
def test_hr_broadcast_reproduces_captured_frames(hexstr: str, enabled: bool, counter: int):
    assert pk.hr_broadcast(enabled, counter).hex() == hexstr


def test_data_retrieval_reproduces_captured_frame():
    assert pk.trigger_data_retrieval(0x0E).hex() == "aa0800a8230e16001147c585"


@pytest.mark.parametrize("hexstr,command,value,counter", [
    ("aa0800a8238c03017d5ec627", pk.CMD_ACTIVITY, 0x01, 0x8C),
    ("aa0800a8238d0300dc040351", pk.CMD_ACTIVITY, 0x00, 0x8D),
    ("aa0800a823d41d003c2e2fe6", pk.CMD_REBOOT, 0x00, 0xD4),
    ("aa0800a8239145 01dd861b95".replace(" ", ""), pk.CMD_ALARM_UNKNOWN_45, 0x01, 0x91),
])
def test_short_command_reproduces_captured_frames(hexstr, command, value, counter):
    assert pk.short_command(command, value, counter).hex() == hexstr


# --- alarm semantics --------------------------------------------------------


def test_alarm_time_is_absolute_little_endian():
    frame = pk.alarm_set(0x11223344, 0)
    assert frame[8:12] == bytes.fromhex("44332211")


def test_alarm_payload_shape():
    frame = pk.alarm_set(1_800_000_000, 0x42)
    assert len(frame) == 20
    payload = pk.decode_frame(frame).payload
    assert payload[0] == pk.TYPE_COMMAND
    assert payload[1] == 0x42
    assert payload[2] == pk.CMD_ALARM_SET
    assert payload[3] == pk.ALARM_SUBCOMMAND
    assert payload[8:12] == b"\x00\x00\x00\x00"


def test_countdown_is_just_an_absolute_time():
    """What makes the nap timer trivial — no separate command needed."""
    now = 1_800_000_000
    frame = pk.alarm_set(now + 20 * 60, 0)
    assert struct.unpack_from("<I", frame, 8)[0] == now + 1200


def test_alarm_rejects_out_of_range_values():
    with pytest.raises(pk.FrameError, match="u32 range"):
        pk.alarm_set(2 ** 32, 0)
    with pytest.raises(pk.FrameError, match="u32 range"):
        pk.alarm_set(-1, 0)
    with pytest.raises(pk.FrameError, match="single byte"):
        pk.alarm_set(1_800_000_000, 256)


# --- refusals ---------------------------------------------------------------


def test_unverified_commands_raise_rather_than_guess():
    """The bytes are unknown, so these must not exist as working functions."""
    with pytest.raises(NotImplementedError, match="0x45"):
        pk.alarm_cancel(0)
    with pytest.raises(NotImplementedError, match="Breathe"):
        pk.haptic_buzz(0)


def test_unverified_registry_explains_each_gap():
    assert set(pk.UNVERIFIED) == {"alarm_cancel", "haptic_buzz", "alarm_readback"}
    assert all(len(v) > 80 for v in pk.UNVERIFIED.values())


# --- validation -------------------------------------------------------------


def test_encode_rejects_empty_and_oversized_payloads():
    with pytest.raises(pk.FrameError, match="empty"):
        pk.encode_frame(b"")
    with pytest.raises(pk.FrameError, match="too long"):
        pk.encode_frame(b"\x00" * (pk.MAX_PAYLOAD + 1))


def test_decode_rejects_a_bad_start_byte():
    frame = bytearray(pk.alarm_set(1_800_000_000, 0))
    frame[0] = 0xBB
    with pytest.raises(pk.FrameError, match="start byte"):
        pk.decode_frame(bytes(frame))


def test_decode_rejects_a_corrupted_header_crc():
    frame = bytearray(pk.alarm_set(1_800_000_000, 0))
    frame[3] ^= 0xFF
    with pytest.raises(pk.FrameError, match="header CRC-8"):
        pk.decode_frame(bytes(frame))


def test_decode_rejects_a_corrupted_payload():
    frame = bytearray(pk.alarm_set(1_800_000_000, 0))
    frame[9] ^= 0xFF
    with pytest.raises(pk.FrameError, match="CRC-32"):
        pk.decode_frame(bytes(frame))


def test_decode_rejects_a_truncated_frame():
    frame = pk.alarm_set(1_800_000_000, 0)
    with pytest.raises(pk.FrameError):
        pk.decode_frame(frame[:-1])
    with pytest.raises(pk.FrameError, match="too short"):
        pk.decode_frame(b"\xaa\x08\x00")


def test_verify_false_parses_without_enforcing_checksums():
    frame = bytearray(pk.alarm_set(1_800_000_000, 0))
    frame[9] ^= 0xFF
    assert pk.decode_frame(bytes(frame), verify=False).command == pk.CMD_ALARM_SET


# --- counter ----------------------------------------------------------------


def test_counter_increments_and_wraps():
    counter = pk.Counter(start=254)
    assert [counter.next() for _ in range(4)] == [254, 255, 0, 1]


# --- describe / parse -------------------------------------------------------


def test_describe_names_known_packet_types():
    assert "live-hr-record" in pk.describe(
        bytes.fromhex("aa1800ff2802ad896566f0654201670600000000000001013ba00d4d"))
    assert "command" in pk.describe(pk.alarm_set(1_800_000_000, 1))


def test_describe_does_not_throw_on_rubbish():
    assert "undecodable" in pk.describe(b"\xaa\x08\x00\xa8garbage!")


def test_parse_live_hr_reads_timestamp_and_bpm():
    frame = bytes.fromhex("aa1800ff2802ad896566f0654201670600000000000001013ba00d4d")
    parsed = pk.parse_live_hr(frame)
    assert parsed == {"ts": 0x666589AD, "bpm": 0x42}


def test_parse_live_hr_ignores_other_packet_types():
    assert pk.parse_live_hr(pk.alarm_set(1_800_000_000, 0)) is None
    assert pk.parse_live_hr(b"nonsense") is None


# --- UUIDs ------------------------------------------------------------------


def test_characteristic_uuids_match_the_documented_table():
    assert pk.CMD_TO_STRAP == "61080002-8d6d-82b8-614a-1c8cb0f8dcc6"
    assert pk.CMD_FROM_STRAP.startswith("61080003")
    assert pk.EVENTS_FROM_STRAP.startswith("61080004")
    assert pk.DATA_FROM_STRAP.startswith("61080005")
    assert pk.MEMFAULT.startswith("61080007")
    assert all(u.endswith(pk.BASE_UUID_SUFFIX) for u in
               (pk.CMD_TO_STRAP, pk.CMD_FROM_STRAP, pk.EVENTS_FROM_STRAP,
                pk.DATA_FROM_STRAP, pk.MEMFAULT))
