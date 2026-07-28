# Phase 4 bench test — connecting your strap, step by step

Read the whole of step 0 before touching anything. One of these steps will
temporarily break the official WHOOP app's pairing, and it is better to know
that going in.

**What this is.** A single script that finds out whether *your* machine can send
a command to *your* strap. It is not the alarm feature; it is the twenty minutes
of proof that decides whether the alarm feature is buildable on `bleak` at all.

**Why it exists.** The packet format is settled — 39/39 captured frames verified,
and `tests/test_ble_packets.py` rebuilds every command byte for byte. The open
question is the transport. The researcher whose captures this is built on reports
that writing to the strap's command characteristic **from a computer via `bleak`
did not work**, and via `gatttool` "works randomly" (`BLE_NOTES.md` §4.2). That is
the exact stack we planned to use, so we test it before building a scheduler on
top of it.

---

## Step 0 — Understand what you are about to do

A WHOOP strap holds an **encrypted Bluetooth bond with one host at a time**. Not
one connection — one *bond*. NOOP's own README is explicit that buzz, alarm,
double-tap and history all require that bond, and that live heart rate is the
exception because it rides the standard Bluetooth heart-rate profile outside the
bond entirely.

Three consequences:

1. **Your phone's WHOOP app almost certainly holds the bond right now.** To pair
   the strap to your computer you have to take it away.
2. **Getting it back means re-pairing the official app afterwards.** This is
   reversible, but it is a real chore, so decide now whether you want to.
3. **Live HR streaming proves nothing.** You can see heart rate on three devices
   at once and still have none of them bonded. If HR works but commands do not,
   that is the tell — not evidence of success.

If you would rather not disturb your phone's pairing, stop here and tell me;
there are narrower things worth trying first (steps 1–4 alone are harmless and
still tell us a lot).

### Which strap do you have?

This matters more than anything else in this document.

| Strap | What to expect |
|---|---|
| **WHOOP 4.0** | Everything in `BLE_NOTES.md` was captured from a 4.0. Best odds. |
| **WHOOP 5.0 / MG** | NOOP says only live HR is confirmed; deeper protocol support "is still being reverse engineered". **The alarm frame is untested on this hardware.** A failure at step 7 may mean the command changed, not that your setup is wrong. |

Tell me which one you have when you send the report — it changes how I read a
failure.

---

## Step 1 — Install the dependency

From the `whoop/` directory, in the same virtualenv you have been using:

```bash
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install bleak
```

`bleak` is the only new dependency. It is not imported anywhere in the dashboard
— only by this script — so nothing you already have changes.

---

## Step 2 — Give your terminal permission to use Bluetooth

**macOS** (most likely, since NOOP is a Mac-first app):

1. System Settings → Privacy & Security → **Bluetooth**
2. Enable the terminal you will run this from — Terminal, iTerm, or VS Code.
   If it is not listed, run the script once; the prompt will appear.
3. **Quit and reopen the terminal.** macOS does not apply this to a running process.

macOS does not expose MAC addresses to apps; you will see a system-generated
UUID like `A1B2C3D4-...` instead. That is normal and the script handles it.

**Linux:**

```bash
systemctl status bluetooth     # should be active
bluetoothctl list              # should print at least one controller
```

If your user is not in the `bluetooth` group:

```bash
sudo usermod -aG bluetooth $USER   # then log out and back in
```

**Windows:** Settings → Bluetooth & devices → Bluetooth on. No extra permission.

---

## Step 3 — Free the strap from everything else

In this order:

1. **On your phone: force-quit the official WHOOP app.** Not backgrounded —
   swipe it away. Better still, turn the phone's Bluetooth off entirely for the
   duration of this test. That is the single most reliable way to be sure.
2. **On your computer: quit NOOP.** Fully quit, not just close the window. If
   NOOP has the bond, this script cannot have it.
3. **Any other paired device** — an iPad, a second phone — same treatment.

To check nothing is holding it, on macOS look at the Bluetooth menu; on Linux:

```bash
bluetoothctl devices Connected
```

---

## Step 4 — Find the strap (harmless, no writes)

```bash
python tools/bench_strap.py --scan
```

You will get a list like:

```
  A1B2C3D4-5E6F-7890-ABCD-EF1234567890   -52 dBm  (unnamed)   <-- likely the strap
  C8:1F:...                              -71 dBm  Some Speaker
```

**The strap frequently advertises with no name.** The script flags a candidate by
the heart-rate service UUID rather than the name, which is why an "(unnamed)"
entry can still be marked as likely.

**If nothing appears:**
- Wake the radio: put the strap on, or tap the sensor face firmly several times.
- Confirm it is charged.
- Re-check step 3 — a strap already connected elsewhere often stops advertising.
- Move within a metre of the machine.

Copy the address of the candidate. If exactly one candidate is found, later
commands can omit `--address` and the script will select it automatically.

---

## Step 5 — Connect and look around (still no writes)

```bash
python tools/bench_strap.py --address <ADDRESS>
```

This connects, enumerates every GATT service, tries a handful of standard reads,
subscribes to notifications, listens for six seconds, and disconnects.

**What a good result looks like:**

```
  [ OK ]  CMD_TO_STRAP (write)         handle 0x0010  [write-without-response]
  [ OK ]  CMD_FROM_STRAP (notify)      handle 0x0012  [notify]
  [ OK ]  EVENTS_FROM_STRAP (notify)   handle 0x0015  [notify]
  [ OK ]  DATA_FROM_STRAP (notify)     handle 0x0018  [notify]
```

**If it connects but reports `NOT PRESENT` for all of them:** the strap is
connected but not *bonded*, so it is not exposing its private service. Go back to
step 3, then see "Pairing properly" below.

This step also answers an open question from Phase 1: **does the strap report a
battery level over the standard Battery Service?** If it does, the dashboard's
battery tile can be filled from BLE even though NOOP does not appear to store it.

---

## Step 6 — The write probe (the deciding test)

```bash
python tools/bench_strap.py --address <ADDRESS> --write-probe
```

This sends **one command, replayed verbatim from a capture** — the
data-retrieval trigger `aa0800a8230e16001147c585`. It changes no setting on the
strap. It was chosen because it is known to provoke a reply, which is the only
real proof a write landed.

Three possible outcomes:

| Outcome | Meaning |
|---|---|
| **Write rejected** (exception) | The stack refused. Almost always a bonding problem — see below. |
| **Write accepted, replies arrive** | **Writes work.** Phase 4 is unblocked. |
| **Write accepted, no reply** | Inconclusive. A write-without-response can succeed locally and be dropped by the strap. Go to step 7 for an answer you can feel. |

---

## Step 7 — The alarm (the answer you can feel)

**Put the strap on your wrist first.** A haptic buzz on a desk is easy to miss,
and a missed buzz reads as a failure.

```bash
python tools/bench_strap.py --address <ADDRESS> --alarm 30
```

This builds a real set-alarm frame for thirty seconds from now, writes it,
prints the exact bytes, and **disconnects immediately** — the alarm lives on the
strap, and holding the connection open is precisely what the design forbids.

Then wait, with the strap on, and watch the clock. The script tells you the time
it should fire.

- **It buzzed** → Phase 4 is fully unblocked. I can build the scheduler, the
  countdown timer, persistence across restarts and the UI, and it will work.
- **It did not buzz** → the write was accepted and ignored. That is a transport
  or bonding problem, not a packet problem — the bytes are proven correct against
  four captured alarm frames. Send me the report and I will tell you which of the
  fallbacks to try.

---

## The report

Every run writes `bench_report.json` in the current directory: platform, `bleak`
version, every device seen, every characteristic found, every notification
captured, and the exact frames sent.

**Send me that file.** It contains BLE addresses of nearby devices and your
strap's firmware/serial strings if they were readable — nothing about your
biometrics, and nothing is uploaded anywhere by the script itself. Skim it first
if you would rather redact the addresses; the parts I need are the `stages`
section and the characteristic list.

---

## If the connection fails

### "Encryption is insufficient" / "bond refused" / connects but no private service

This is the big one, and it means the strap is still bonded elsewhere.

**Pairing properly:**

1. Force-quit the WHOOP app on your phone, or turn that phone's Bluetooth off.
2. Put the strap into pairing mode. On a **5.0/MG**, tap the band repeatedly —
   firm taps on the sensor — until the **LEDs flash blue**.
3. Pair from your computer:
   - **macOS:** it may pair implicitly on connect; if not, System Settings →
     Bluetooth and pair the device there first.
   - **Linux:** `bluetoothctl` → `scan on` → `pair <ADDRESS>` → `trust <ADDRESS>`
     → `connect <ADDRESS>`
   - **Windows:** Settings → Bluetooth & devices → Add device.
4. Re-run step 5. It can take a couple of attempts.

Expect the official app to need re-pairing afterwards.

### macOS: stale pairing cache

macOS caches BLE pairings and can hold a broken one. Remove the device from
System Settings → Bluetooth, toggle Bluetooth off and on, then retry.

### Linux: pairing agent problems

```bash
bluetoothctl
> power on
> agent on
> default-agent
> scan on
> pair <ADDRESS>
> trust <ADDRESS>
```

If BlueZ refuses with an authentication error, `sudo systemctl restart bluetooth`
and retry.

### It works once, then stops

Consistent with the "works randomly" behaviour in the source writeup. Note how
many attempts out of how many succeeded in your report — a flaky-but-workable
transport changes the design (retry, verify, surface failures loudly) but does
not kill the feature.

---

## What happens next

| Bench result | What I build |
|---|---|
| Alarm buzzes reliably | Phase 4 in full on `bleak`: scheduler, countdown timer, persistence across restarts, list/edit/cancel, connection-state UI. |
| Alarm buzzes intermittently | Same, plus a verify-and-retry loop and prominent failure reporting. The "did NOT get set" case becomes a first-class UI state rather than an edge case. |
| Write accepted, never buzzes | I add a `gatttool`/`bluetoothctl` transport behind the same interface and we re-test. The packet builders do not change. |
| Cannot bond at all | Phase 4 stalls honestly. I would build the scheduler and UI against a transport stub so the work is not wasted, mark alarms as unavailable in the UI, and we revisit if the pairing situation changes. I would not fake it. |

Whatever the outcome, **cancel remains unimplemented** — no capture shows an
alarm being cancelled, so the scheduler will drop a pending alarm from its own
list and tell you plainly that it could not un-set it on the strap. See
`BLE_NOTES.md` §3.3.
