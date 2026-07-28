#!/usr/bin/env python3
"""Verify the WHOOP frame format in BLE_NOTES.md against captured packets.

Every packet below is transcribed from bWanShiTong/reverse-engineering-whoop-post
@ 305e35d (Wireshark/btsnoop captures of a WHOOP 4.0). Nothing here is synthetic.

Run:  python tools/verify_ble_frame.py
Exit: 0 if every check passes, 1 otherwise.

This is the evidence for the claims in BLE_NOTES.md sections 2 and 2.3. If a
future capture contradicts them, add it here first.
"""

from __future__ import annotations

import struct
import sys
import zlib

# --- captured frames, grouped by what produced them -------------------------

CAPTURES: dict[str, list[str]] = {
    "alarm set (0x42)": [
        "aa100057236d4201d036656600000000f62deb81",
        "aa100057236e42010c376566000000001023ccef",
        "aa100057236f4201207d656600000000fea1e060",
        "aa100057237042015011656600000000f226a8bd",
        "aa10005723814201f07e6666000000007037c2a4",
        "aa10005723824201f07e6666000000007151203d",
        "aa10005723834201f07e666600000000b18eaefc",
    ],
    "hr broadcast toggle (0x0e)": [
        "aa0800a823070e00c7e40f08",
        "aa0800a823080e016c935474",
        "aa0800a823090e00cdc99102",
    ],
    "activity start/stop (0x03)": [
        "aa0800a8238c03017d5ec627",
        "aa0800a8238d0300dc040351",
        "aa0800a8239003016904fa32",
        "aa0800a823910300c85e3f44",
        "aa0800a8238f0300b2d08752",
        "aa0800a823050300e44e25be",
        "aa0800a8230603012bc064cb",
    ],
    "misc commands": [
        "aa0800a8236674013ae4cde3",   # 0x74
        "aa0800a8239145 01dd861b95".replace(" ", ""),  # 0x45 "alarm off"
        "aa0800a8230e16001147c585",   # 0x16 data retrieval
        "aa0800a82315730 1f4a43bfa".replace(" ", ""),  # 0x73 enable
        "aa0800a82316740 16a8c3cb7".replace(" ", ""),  # 0x74 enable
    ],
    "reboot (0x1d)": [
        "aa0800a823d41d003c2e2fe6",
        "aa0800a823da1d003603b1ec",
        "aa0800a823df1d00ddc17aea",
        "aa0800a823e21d001eb7c9c6",
    ],
    "erase (0x19)": [
        "aa10005723cf19fefefefefefefefe002f8744f6",
        "aa10005723d219fefefefefefefefe00e30e2693",
        "aa10005723d319fefefefefefefefe0023d1a852",
    ],
    "live hr record (0x28)": [
        "aa1800ff2802ad896566f0654201670600000000000001013ba00d4d",
        "aa1800ff2802ae896566f8604300000000000000000001015025f793",
        "aa1800ff2802af896566085c420000000000000000000101add7df13",
        "aa1800ff2802b0896566105742000000000000000000010124b22179",
    ],
    "sync batch descriptor (0x31)": [
        "aa1c00ab311802f65c70668040430000002e4701000400000000000 07f873cf3".replace(" ", ""),
        "aa1c00ab311902fb5c70667041430000002e470100040000000000 00f277ceb0".replace(" ", ""),
    ],
    "event record (0x30)": [
        "aa100057305b21003f32696668540000b0b2435b",
        "aa1000573065220045326966a8660000093b5aa6",
        "aa10005730661800483269663012 0000ef5360f0".replace(" ", ""),
        "aa10005730811800ba3269662873 00009b28989f".replace(" ", ""),
    ],
}

# Longer frames whose header CRC-8 we check but whose payload we don't have in full.
HEADER_ONLY: list[tuple[int, int]] = [
    (36, 0xFA),   # aa2400fa30 — event, EVENTS_FROM_STRAP
    (72, 0xF3),   # aa4800f323 — "random text" command
    (92, 0xF0),   # aa5c00f02f — 96-byte history sample
]


def crc8_smbus(data: bytes) -> int:
    """CRC-8/SMBUS: poly 0x07, init 0x00, no reflection, xorout 0x00."""
    crc = 0x00
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = ((crc << 1) ^ 0x07) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


def crc32_whoop_published(data: bytes) -> int:
    """The parameters published by [RE]: poly 0x04C11DB7 reflected, init 0, xorout 0xF43F44AC.

    Kept only to demonstrate that they are a degenerate fit (see BLE_NOTES.md 2.3).
    """
    crc = 0x00000000
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xEDB88320 if crc & 1 else crc >> 1
    return (crc ^ 0xF43F44AC) & 0xFFFFFFFF


def main() -> int:
    frames = [(group, bytes.fromhex(hexstr))
              for group, hexstrs in CAPTURES.items() for hexstr in hexstrs]
    failures: list[str] = []

    print(f"WHOOP frame verification — {len(frames)} captured packets\n")

    # 1. length field
    print("1. total_length == len_u16_le + 4")
    for group, raw in frames:
        declared = struct.unpack_from("<H", raw, 1)[0]
        if len(raw) != declared + 4:
            failures.append(f"length: {raw.hex()} declared={declared} actual={len(raw)}")
    print(f"   {'PASS' if not failures else 'FAIL'} — {len(frames)}/{len(frames)}\n")

    # 2. header CRC-8
    print("2. byte[3] == CRC-8/SMBUS(len_u16_le)")
    hdr_fail = 0
    lengths_seen: dict[int, int] = {}
    for _group, raw in frames:
        declared = struct.unpack_from("<H", raw, 1)[0]
        want = crc8_smbus(raw[1:3])
        lengths_seen[declared] = raw[3]
        if raw[3] != want:
            hdr_fail += 1
            failures.append(f"hdr_crc: {raw.hex()} want={want:02x} got={raw[3]:02x}")
    for length, observed in HEADER_ONLY:
        want = crc8_smbus(struct.pack("<H", length))
        lengths_seen[length] = observed
        if observed != want:
            hdr_fail += 1
            failures.append(f"hdr_crc(len={length}) want={want:02x} got={observed:02x}")
    for length in sorted(lengths_seen):
        print(f"   len={length:<3} -> 0x{lengths_seen[length]:02x}")
    print(f"   {'PASS' if not hdr_fail else 'FAIL'} — "
          f"{len(lengths_seen)} distinct lengths\n")

    # 3. trailing CRC-32
    print("3. trailing u32 LE == standard CRC-32 over frame[4:-4]")
    width = max(len(g) for g in CAPTURES)
    total_ok = 0
    for group in CAPTURES:
        group_frames = [raw for g, raw in frames if g == group]
        ok = sum(1 for raw in group_frames
                 if zlib.crc32(raw[4:-4]) == struct.unpack_from("<I", raw, len(raw) - 4)[0])
        total_ok += ok
        flag = "ok " if ok == len(group_frames) else "FAIL"
        print(f"   {flag} {group:<{width}}  {ok}/{len(group_frames)}")
        if ok != len(group_frames):
            failures.append(f"crc32 group {group}: {ok}/{len(group_frames)}")
    print(f"   {'PASS' if total_ok == len(frames) else 'FAIL'} — "
          f"{total_ok}/{len(frames)}\n")

    # 4. the published parameters, for the record
    published_ok = sum(
        1 for _g, raw in frames
        if crc32_whoop_published(raw[:-4]) == struct.unpack_from("<I", raw, len(raw) - 4)[0]
    )
    print("4. [RE]'s published CRC params (init 0x0, xorout 0xF43F44AC, whole frame)")
    print(f"   {published_ok}/{len(frames)} — degenerate fit, see BLE_NOTES.md 2.3\n")

    if failures:
        print(f"FAILED ({len(failures)}):")
        for f in failures:
            print(f"  {f}")
        return 1

    print(f"All checks passed: {len(frames)}/{len(frames)} frames, "
          f"{len(lengths_seen)} distinct lengths.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
