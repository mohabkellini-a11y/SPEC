#!/usr/bin/env python3
"""Bench test: can this machine actually talk to the strap?

Phase 4 rests on one unproven assumption. The packet format is verified (39/39
captured frames, and tests/test_ble_packets.py rebuilds every command byte for
byte) — but the community researcher this is built on reports that writing to
CMD_TO_STRAP from a computer *via bleak* did not work, and via gatttool "works
randomly". See BLE_NOTES.md §4.2.

So before any scheduler gets written, this script answers, on your hardware:

    1. Can we see the strap?
    2. Can we connect?
    3. Are the documented characteristics actually there?
    4. Does the strap report a battery level? (SCHEMA_NOTES.md open question 4)
    5. Do notifications arrive?
    6. Does a WRITE land?              <- the one that decides Phase 4
    7. Does an alarm actually buzz?    <- only with --alarm

Nothing is written unless you pass a flag. The connection is always closed on
the way out, including on Ctrl-C — the strap bonds to one host at a time and
holding it idle would lock NOOP out for no reason.

Usage
-----
    python tools/bench_strap.py --scan
    python tools/bench_strap.py --address XX:XX:XX:XX:XX:XX
    python tools/bench_strap.py --address XX:XX:XX:XX:XX:XX --write-probe
    python tools/bench_strap.py --address XX:XX:XX:XX:XX:XX --alarm 30

Requires: pip install bleak
"""

from __future__ import annotations

import argparse
import asyncio
import json
import platform
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.ble import packets as pk  # noqa: E402

try:
    from bleak import BleakClient, BleakScanner
except ImportError:  # pragma: no cover - environment-dependent
    print("bleak is not installed.\n\n    pip install bleak\n", file=sys.stderr)
    raise SystemExit(2)


NAME_HINTS = ("whoop", "wrist", "strap")
SCAN_SECONDS = 8.0
CONNECT_TIMEOUT = 20.0
NOTIFY_WINDOW = 6.0


class Report:
    """Collects findings so a failed run still produces something useful."""

    def __init__(self) -> None:
        self.data: dict = {
            "generated_at": datetime.now(tz=timezone.utc).isoformat(),
            "platform": {
                "system": platform.system(),
                "release": platform.release(),
                "python": platform.python_version(),
            },
            "stages": {},
        }
        try:
            import bleak
            self.data["platform"]["bleak"] = bleak.__version__
        except Exception:  # noqa: BLE001
            self.data["platform"]["bleak"] = "unknown"

    def stage(self, name: str, ok: bool, **detail) -> None:
        self.data["stages"][name] = {"ok": ok, **detail}

    def save(self, path: Path) -> None:
        path.write_text(json.dumps(self.data, indent=2, default=str))


def say(text: str = "") -> None:
    print(text, flush=True)


def adapter_hint(exc: Exception) -> list[str]:
    """Turn an obscure stack-level failure into the thing you actually do next.

    bleak surfaces missing-adapter and permission problems as plain OS errors,
    so the raw message ("No such file or directory") says nothing useful.
    """
    system = platform.system()
    text = f"{type(exc).__name__}: {exc}".lower()

    if isinstance(exc, FileNotFoundError) or "no such file" in text:
        if system == "Linux":
            return [
                "That is D-Bus refusing the connection — almost always no BlueZ.",
                "Check with:  systemctl status bluetooth  and  bluetoothctl list",
                "Inside a container or VM, the host's Bluetooth adapter is usually",
                "not passed through. Run this on the host instead.",
            ]
        return ["No Bluetooth adapter appears to be available to this process."]

    if system == "Darwin" and ("unauthorized" in text or "denied" in text or "authoriz" in text):
        return [
            "macOS is blocking Bluetooth for your terminal.",
            "System Settings -> Privacy & Security -> Bluetooth -> enable your",
            "terminal (Terminal, iTerm, VS Code...). Then restart the terminal.",
        ]

    if system == "Linux" and ("permission" in text or "access denied" in text):
        return [
            "Permission denied by BlueZ. Either add yourself to the bluetooth group",
            "(sudo usermod -aG bluetooth $USER, then log out and back in), or run",
            "this once with sudo to confirm that is the cause.",
        ]

    if "adapter" in text or "not turned on" in text or "poweredoff" in text:
        return ["Bluetooth appears to be switched off. Turn it on and retry."]

    return ["See docs/PHASE4_BENCH.md, 'If the connection fails'."]


def head(n: int, title: str) -> None:
    say(f"\n{'=' * 62}\n  STAGE {n}: {title}\n{'=' * 62}")


def ok(text: str) -> None:
    say(f"  [ OK ]  {text}")


def bad(text: str) -> None:
    say(f"  [FAIL]  {text}")


def info(text: str) -> None:
    say(f"          {text}")


# --- stage 1: scan ----------------------------------------------------------


async def stage_scan(report: Report, seconds: float) -> list:
    head(1, "Scan for BLE devices")
    info(f"Scanning for {seconds:.0f}s. The strap must be awake — wear it, or")
    info("tap it a few times to wake the radio.")
    say()

    try:
        devices = await BleakScanner.discover(timeout=seconds, return_adv=True)
    except Exception as exc:  # noqa: BLE001 - bleak raises OS errors, not just BleakError
        bad(f"Scan failed: {type(exc).__name__}: {exc}")
        say()
        for line in adapter_hint(exc):
            info(line)
        report.stage("scan", False, error=f"{type(exc).__name__}: {exc}")
        return []

    rows = []
    for device, adv in devices.values():
        name = device.name or adv.local_name or ""
        uuids = [u.lower() for u in (adv.service_uuids or [])]
        likely = (
            any(hint in name.lower() for hint in NAME_HINTS)
            or pk.HEART_RATE_SERVICE.lower() in uuids
            or any(u.startswith("61080") for u in uuids)
        )
        rows.append({
            "address": device.address,
            "name": name or "(unnamed)",
            "rssi": adv.rssi,
            "service_uuids": uuids,
            "likely_strap": likely,
        })

    rows.sort(key=lambda r: (not r["likely_strap"], -(r["rssi"] or -999)))
    if not rows:
        bad("No BLE devices found at all — check Bluetooth is on and permitted.")
    for row in rows[:25]:
        mark = " <-- likely the strap" if row["likely_strap"] else ""
        say(f"  {row['address']}  {row['rssi']:>4} dBm  {row['name'][:28]:<28}{mark}")

    candidates = [r for r in rows if r["likely_strap"]]
    say()
    if candidates:
        ok(f"{len(candidates)} likely candidate(s).")
        info("A WHOOP strap often advertises with no name — the giveaway is the")
        info(f"heart-rate service {pk.HEART_RATE_SERVICE[:8]}.")
    else:
        bad("Nothing looks like a WHOOP strap.")
        info("If the official app or NOOP is connected, the strap may not be")
        info("advertising. Close them and try again.")

    report.stage("scan", bool(rows), devices=rows, candidates=len(candidates))
    return rows


# --- stage 2-3: connect + enumerate ----------------------------------------


EXPECTED = {
    pk.CMD_TO_STRAP: "CMD_TO_STRAP (write)",
    pk.CMD_FROM_STRAP: "CMD_FROM_STRAP (notify)",
    pk.EVENTS_FROM_STRAP: "EVENTS_FROM_STRAP (notify)",
    pk.DATA_FROM_STRAP: "DATA_FROM_STRAP (notify)",
    pk.MEMFAULT: "MEMFAULT (notify)",
}


async def stage_enumerate(client: BleakClient, report: Report) -> dict:
    head(3, "Enumerate GATT services")

    found: dict[str, dict] = {}
    for service in client.services:
        for char in service.characteristics:
            found[char.uuid.lower()] = {
                "service": service.uuid,
                "properties": list(char.properties),
                "handle": char.handle,
                "description": char.description,
            }

    say(f"  {len(found)} characteristics across {len(list(client.services))} services.\n")
    say("  Documented WHOOP characteristics (BLE_NOTES.md §1):")
    present = 0
    for uuid, label in EXPECTED.items():
        entry = found.get(uuid.lower())
        if entry:
            present += 1
            props = ",".join(entry["properties"])
            ok(f"{label:<28} handle 0x{entry['handle']:04x}  [{props}]")
        else:
            bad(f"{label:<28} NOT PRESENT")

    say("\n  Standard profiles:")
    for uuid, label in ((pk.HEART_RATE_MEASUREMENT, "Heart Rate Measurement"),
                        (pk.BATTERY_LEVEL, "Battery Level"),
                        ("00002a29-0000-1000-8000-00805f9b34fb", "Manufacturer Name"),
                        ("00002a24-0000-1000-8000-00805f9b34fb", "Model Number"),
                        ("00002a26-0000-1000-8000-00805f9b34fb", "Firmware Revision")):
        (ok if uuid.lower() in found else info)(
            f"{label:<28} {'present' if uuid.lower() in found else 'absent'}")

    report.stage("enumerate", present > 0, characteristics=found,
                 documented_present=present, documented_expected=len(EXPECTED))
    if present == 0:
        say()
        bad("None of the documented characteristics are here.")
        info("Either this is not a WHOOP strap, or the strap has not exposed its")
        info("private service because it is not bonded to this machine.")
    elif present < len(EXPECTED):
        say()
        info(f"Only {present} of {len(EXPECTED)} documented characteristics present —")
        info("worth noting in the report; the protocol may differ on your model.")
    return found


# --- stage 4: reads ---------------------------------------------------------


async def stage_reads(client: BleakClient, found: dict, report: Report) -> dict:
    head(4, "Read what the strap will tell us")
    result: dict = {}

    readable = {
        "battery_percent": (pk.BATTERY_LEVEL, lambda b: int(b[0]) if b else None),
        "manufacturer": ("00002a29-0000-1000-8000-00805f9b34fb", lambda b: b.decode(errors="replace")),
        "model": ("00002a24-0000-1000-8000-00805f9b34fb", lambda b: b.decode(errors="replace")),
        "firmware": ("00002a26-0000-1000-8000-00805f9b34fb", lambda b: b.decode(errors="replace")),
        "serial": ("00002a25-0000-1000-8000-00805f9b34fb", lambda b: b.decode(errors="replace")),
    }
    for label, (uuid, decode) in readable.items():
        if uuid.lower() not in found:
            info(f"{label:<16} not exposed")
            continue
        try:
            raw = await client.read_gatt_char(uuid)
            value = decode(raw)
            result[label] = value
            ok(f"{label:<16} {value}")
        except Exception as exc:  # noqa: BLE001
            result[label] = None
            bad(f"{label:<16} read failed: {exc}")

    if "battery_percent" in result and result["battery_percent"] is not None:
        say()
        ok("Battery IS readable over the standard Battery Service.")
        info("That answers SCHEMA_NOTES.md open question 4 — the dashboard tile")
        info("can be filled from BLE even though NOOP does not appear to store it.")

    report.stage("reads", bool(result), values=result)
    return result


# --- stage 5: notifications -------------------------------------------------


async def stage_notify(client: BleakClient, found: dict, report: Report) -> list:
    head(5, "Subscribe to notifications")
    captured: list[dict] = []

    def make_handler(source: str):
        def handler(_sender, data: bytearray) -> None:
            frame = bytes(data)
            captured.append({"source": source, "hex": frame.hex(),
                             "described": pk.describe(frame),
                             "at": time.time()})
            say(f"    <- {source}: {pk.describe(frame)}")
        return handler

    subscribed = []
    for uuid, label in ((pk.CMD_FROM_STRAP, "CMD_FROM_STRAP"),
                        (pk.DATA_FROM_STRAP, "DATA_FROM_STRAP"),
                        (pk.EVENTS_FROM_STRAP, "EVENTS_FROM_STRAP"),
                        (pk.HEART_RATE_MEASUREMENT, "HeartRate")):
        if uuid.lower() not in found:
            continue
        try:
            await client.start_notify(uuid, make_handler(label))
            subscribed.append((uuid, label))
            ok(f"subscribed to {label}")
        except Exception as exc:  # noqa: BLE001
            bad(f"could not subscribe to {label}: {exc}")

    if not subscribed:
        bad("No notify characteristics available.")
        report.stage("notify", False, captured=[])
        return []

    say(f"\n  Listening {NOTIFY_WINDOW:.0f}s for unprompted traffic...")
    await asyncio.sleep(NOTIFY_WINDOW)
    if not captured:
        info("Nothing arrived. Not necessarily wrong — the strap may be idle.")
    else:
        ok(f"{len(captured)} notification(s) received.")

    report.stage("notify", True, subscribed=[label for _u, label in subscribed],
                 captured=captured)
    return subscribed


# --- stage 6: write probe ---------------------------------------------------


async def stage_write_probe(client: BleakClient, found: dict, report: Report,
                            before_count: int, captured: list) -> bool:
    head(6, "Write probe  <-- THE DECIDING TEST")

    if pk.CMD_TO_STRAP.lower() not in found:
        bad("CMD_TO_STRAP is not present; nothing to write to.")
        report.stage("write_probe", False, reason="characteristic absent")
        return False

    props = found[pk.CMD_TO_STRAP.lower()]["properties"]
    with_response = "write" in props
    info(f"CMD_TO_STRAP properties: {','.join(props)}")
    info(f"Using write {'WITH' if with_response else 'WITHOUT'} response.")

    counter = pk.Counter(start=1)
    frame = pk.trigger_data_retrieval(counter.next())
    info(f"Sending the captured data-retrieval command (changes no setting):")
    info(f"  {frame.hex()}")
    say()

    try:
        await client.write_gatt_char(pk.CMD_TO_STRAP, frame, response=with_response)
    except Exception as exc:  # noqa: BLE001
        bad(f"WRITE REJECTED: {type(exc).__name__}: {exc}")
        say()
        info("This is the failure mode BLE_NOTES.md §4.2 warns about.")
        info("Most likely the strap is not bonded to this machine. See the")
        info("'If the write is rejected' section of docs/PHASE4_BENCH.md.")
        report.stage("write_probe", False, error=str(exc), frame=frame.hex())
        return False

    ok("Write accepted by the stack (no exception).")
    info("That is necessary but NOT sufficient — a write-without-response can")
    info("succeed locally and still be dropped by the strap. Watching for a")
    info(f"reply for {NOTIFY_WINDOW:.0f}s, which is the real proof...")
    say()

    await asyncio.sleep(NOTIFY_WINDOW)
    new = len(captured) - before_count
    if new > 0:
        ok(f"{new} notification(s) arrived after the write.")
        info("The strap acted on it. WRITES WORK ON THIS MACHINE.")
        report.stage("write_probe", True, accepted=True, replies=new, frame=frame.hex())
        return True

    bad("No reply. The write was accepted locally but the strap did not respond.")
    info("Inconclusive rather than definitely broken — this command may simply")
    info("have had nothing to send. Try --alarm, which you can feel.")
    report.stage("write_probe", True, accepted=True, replies=0,
                 verdict="inconclusive", frame=frame.hex())
    return True


# --- stage 7: alarm ---------------------------------------------------------


async def stage_alarm(client: BleakClient, found: dict, report: Report,
                      seconds: int) -> None:
    head(7, f"Set a real alarm, {seconds}s from now")

    if pk.CMD_TO_STRAP.lower() not in found:
        bad("CMD_TO_STRAP absent; cannot set an alarm.")
        report.stage("alarm", False, reason="characteristic absent")
        return

    when = int(time.time()) + seconds
    frame = pk.alarm_set(when, pk.Counter(start=0x70).next())
    fires_at = datetime.fromtimestamp(when).strftime("%H:%M:%S")

    info(f"Alarm time : {fires_at} (unix {when})")
    info(f"Frame      : {frame.hex()}")
    info("Layout     : AA 10 00 57 | 23 ctr 42 01 | u32 LE time | 4x00 | crc32")
    say()
    info("PUT THE STRAP ON YOUR WRIST — a haptic on a desk is easy to miss.")
    say()

    props = found[pk.CMD_TO_STRAP.lower()]["properties"]
    try:
        await client.write_gatt_char(pk.CMD_TO_STRAP, frame, response="write" in props)
        ok("Alarm frame written.")
    except Exception as exc:  # noqa: BLE001
        bad(f"WRITE REJECTED: {type(exc).__name__}: {exc}")
        report.stage("alarm", False, error=str(exc), frame=frame.hex(), when=when)
        return

    say()
    info("Disconnecting now — the strap keeps the alarm on-device, and holding")
    info("the connection open is exactly what the design forbids.")
    report.stage("alarm", True, written=True, frame=frame.hex(), when=when,
                 fires_at=fires_at,
                 note="Whether it buzzed must be confirmed by the human.")


# --- orchestration ----------------------------------------------------------


async def run(args: argparse.Namespace, report: Report) -> int:
    rows = []
    if args.scan or not args.address:
        rows = await stage_scan(report, args.scan_seconds)
        if not args.address:
            candidates = [r for r in rows if r["likely_strap"]]
            if len(candidates) == 1:
                args.address = candidates[0]["address"]
                say(f"\n  Auto-selecting {args.address}")
            elif candidates:
                say("\n  More than one candidate. Re-run with the right one:")
                for row in candidates:
                    say(f"    python tools/bench_strap.py --address {row['address']}")
                return 0
            else:
                say("\n  Nothing to connect to. Fix the scan first, then re-run")
                say("  with --address once the strap appears in the list.")
                return 1

    head(2, f"Connect to {args.address}")
    info("A WHOOP strap holds an encrypted bond with ONE host at a time.")
    info("If NOOP or the official app has it, this will fail or connect without")
    info("exposing the private service.")
    say()

    client = BleakClient(args.address, timeout=CONNECT_TIMEOUT)
    connected = False
    try:
        try:
            await client.connect()
            connected = True
            ok(f"Connected to {args.address}")
            report.stage("connect", True, address=args.address)
        except Exception as exc:  # noqa: BLE001
            bad(f"Connect failed: {type(exc).__name__}: {exc}")
            say()
            text = str(exc).lower()
            if "encryption" in text or "insufficient" in text or "bond" in text:
                info("'Encryption is insufficient' / 'bond refused' is the signature")
                info("of a strap still bonded to another host. Close the official")
                info("WHOOP app and NOOP, then pair to this machine — see")
                info("docs/PHASE4_BENCH.md step 3.")
            else:
                for line in adapter_hint(exc):
                    info(line)
            report.stage("connect", False, address=args.address,
                         error=f"{type(exc).__name__}: {exc}")
            return 1

        found = await stage_enumerate(client, report)
        await stage_reads(client, found, report)

        captured: list = []
        subscribed = []
        if not args.no_notify:
            subscribed = await stage_notify(client, found, report)
            captured = report.data["stages"].get("notify", {}).get("captured", [])

        if args.write_probe or args.alarm:
            await stage_write_probe(client, found, report, len(captured), captured)

        if args.alarm:
            await stage_alarm(client, found, report, args.alarm)

        for uuid, _label in subscribed:
            try:
                await client.stop_notify(uuid)
            except Exception:  # noqa: BLE001, S110
                pass

    finally:
        # The one rule that must never be broken: never leave the radio held.
        if connected:
            try:
                await client.disconnect()
                say("\n  Disconnected. The strap is released.")
            except Exception as exc:  # noqa: BLE001
                say(f"\n  Disconnect raised {exc} — the OS will drop the link.")
    return 0


def summarise(report: Report, args: argparse.Namespace) -> None:
    stages = report.data["stages"]
    say(f"\n{'=' * 62}\n  SUMMARY\n{'=' * 62}")
    for name, detail in stages.items():
        say(f"  {'PASS' if detail.get('ok') else 'FAIL'}  {name}")

    say()
    if not stages.get("scan", {}).get("ok") and "connect" not in stages:
        say("  Verdict: the Bluetooth stack itself did not work — we never got as")
        say("  far as the strap. Fix that first; see the hint above.")
    elif "connect" not in stages:
        say("  Verdict: scan only. Re-run with --address to go further.")
    elif not stages.get("connect", {}).get("ok"):
        say("  Verdict: could not connect. Phase 4 is blocked until this works.")
        say("  Next: docs/PHASE4_BENCH.md, 'If the connection fails'.")
    elif stages.get("enumerate", {}).get("documented_present", 0) == 0:
        say("  Verdict: connected, but the private service is not exposed.")
        say("  That is the signature of an unbonded strap. Pair it first.")
    elif stages.get("write_probe", {}).get("replies", 0) > 0:
        say("  Verdict: WRITES WORK. Phase 4 can proceed on bleak.")
    elif "alarm" in stages and stages["alarm"].get("ok"):
        say(f"  Verdict: alarm frame written. Did the strap buzz at "
            f"{stages['alarm'].get('fires_at')}?")
        say("  If yes  -> Phase 4 is unblocked; the full scheduler is buildable.")
        say("  If no   -> the write was accepted but ignored. Send me the report;")
        say("             the transport needs a different backend, not new bytes.")
    elif "write_probe" in stages:
        say("  Verdict: inconclusive. Re-run with --alarm 30 to get a felt answer.")
    else:
        say("  Verdict: read-only probe complete. Re-run with --write-probe.")

    if args.report:
        report.save(Path(args.report))
        say(f"\n  Report written to {args.report}")
        say("  Send me that file and I can tell you exactly what happened.")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Bench-test BLE connectivity to a WHOOP strap you own.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Nothing is written to the strap unless --write-probe or --alarm.",
    )
    parser.add_argument("--address", help="strap BLE address (or UUID on macOS)")
    parser.add_argument("--scan", action="store_true", help="scan and exit")
    parser.add_argument("--scan-seconds", type=float, default=SCAN_SECONDS)
    parser.add_argument("--write-probe", action="store_true",
                        help="send one harmless captured command and watch for a reply")
    parser.add_argument("--alarm", type=int, metavar="SECONDS",
                        help="set a real alarm this many seconds from now (try 30)")
    parser.add_argument("--no-notify", action="store_true",
                        help="skip the notification stage")
    parser.add_argument("--report", default="bench_report.json",
                        help="where to write the JSON report (default: bench_report.json)")
    args = parser.parse_args()

    if args.alarm is not None and not (5 <= args.alarm <= 600):
        parser.error("--alarm must be between 5 and 600 seconds")

    say("WHOOP strap bench test")
    say("Interoperability probe for a device you own. Nothing is uploaded.")

    report = Report()
    try:
        code = asyncio.run(run(args, report))
    except KeyboardInterrupt:
        say("\n\n  Interrupted — the connection is closed.")
        code = 130
    summarise(report, args)
    return code


if __name__ == "__main__":
    sys.exit(main())
