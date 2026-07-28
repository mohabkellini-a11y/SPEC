# BLE_NOTES.md — WHOOP strap GATT + packet format

Investigation output for Step 0.2. Written 2026-07-28.

Every claim below carries a citation. Where I could not verify something from a
source, it says **UNVERIFIED — STUBBED** and there is no code that pretends
otherwise.

## Sources

| Tag | Source |
|-----|--------|
| **[RE]** | `github.com/bWanShiTong/reverse-engineering-whoop-post` @ `305e35d` — "Reverse Engineering Whoop 4.0 for fun and FREEDOM". Wireshark/btsnoop captures against a **WHOOP 4.0** and a decompiled official APK. |
| **[NOOP]** | `github.com/noop-app/noop` @ `d97fb89` — README only. NOOP's actual BLE layer (`Strand/BLE`, `WhoopProtocol`) is **not published**, so it could not corroborate any byte layout. |
| **[HERE]** | Original analysis done in this repo: `tools/verify_ble_frame.py`. Re-runnable; prints pass/fail per packet. |

---

## 1. GATT services and characteristics — [RE], high confidence

Custom service, 128-bit UUIDs of the form `6108000X-8d6d-82b8-614a-1c8cb0f8dcc6`.
The full UUID appears verbatim in [RE]'s pygatt example
(`device.char_write("61080002-8d6d-82b8-614a-1c8cb0f8dcc6", ...)`); the short forms
and the names come from [RE]'s table, which it states were taken from the
decompiled official APK.

| Short | Full UUID | Name | Props | Handle |
|-------|-----------|------|-------|--------|
| `61080002` | `61080002-8d6d-82b8-614a-1c8cb0f8dcc6` | `CMD_TO_STRAP` | write | `0x0010` |
| `61080003` | `61080003-8d6d-82b8-614a-1c8cb0f8dcc6` | `CMD_FROM_STRAP` | notify | `0x0012` |
| `61080004` | `61080004-8d6d-82b8-614a-1c8cb0f8dcc6` | `EVENTS_FROM_STRAP` | notify | `0x0015` |
| `61080005` | `61080005-8d6d-82b8-614a-1c8cb0f8dcc6` | `DATA_FROM_STRAP` | notify | `0x0018` |
| `61080007` | `61080007-8d6d-82b8-614a-1c8cb0f8dcc6` | `MEMFAULT` | notify | `0x001b` |

The service UUID itself is **not stated** in [RE] — only the characteristics. By
the pattern it is almost certainly `61080001-8d6d-82b8-614a-1c8cb0f8dcc6`, but
that is an inference; the code discovers characteristics by UUID rather than
assuming a service UUID.

**`CMD_TO_STRAP` is the only writable characteristic.** Everything we send goes there.

### Standard heart-rate profile
The strap also exposes the standard Bluetooth Heart Rate service — [RE] reads it
with `00002a37-0000-1000-8000-00805f9b34fb` (Heart Rate Measurement). [NOOP]'s
README confirms this rides *outside* the encrypted bond: *"Live heart rate is the
exception — it rides the standard Bluetooth heart-rate profile, so it streams
without a bond."*

---

## 2. Frame format — [HERE], verified

[RE] describes the framing loosely and gets the checksum parameters wrong (§2.3).
I re-derived the framing from its captured packets and verified it exhaustively.

```
 byte:  0      1        2       3         4 ................ n-5   n-4 .. n-1
      +------+--------+--------+--------+---------------------+-------------+
      | 0xAA | len_lo | len_hi | hdr_crc|      payload        |  crc32 LE   |
      +------+--------+--------+--------+---------------------+-------------+
              \___ u16 LE ____/
```

- `0xAA` — start of frame. Constant across all 39 captured packets.
- `len` — **u16 little-endian**, and `total_frame_length == len + 4`. Holds for
  every captured packet across 4 distinct lengths (8, 16, 24, 28) and the longer
  36/72/92-byte forms. `len` counts the payload **plus** the 4-byte trailing CRC.
- `hdr_crc` — **CRC-8/SMBUS** over the two length bytes: poly `0x07`, init `0x00`,
  no reflection, xorout `0x00`. Verified against all 7 distinct observed lengths
  (8→`0xa8`, 16→`0x57`, 24→`0xff`, 28→`0xab`, 36→`0xfa`, 72→`0xf3`, 92→`0xf0`).
  A brute-force over all 256 polys × 256 inits × {msb,lsb} × {0x00,0xff} xorouts
  returned 4 solutions, of which this is the canonical/textbook one and the other
  three are algebraic aliases.
- `crc32` — **standard CRC-32** (the zlib/PKZIP one: poly `0x04C11DB7` reflected,
  init `0xFFFFFFFF`, xorout `0xFFFFFFFF`), computed over `frame[4:-4]`, stored
  **little-endian**.

**Verified: 39 / 39 captured packets**, spanning commands, alarm sets, streamed HR
records, sync-batch records, event records, erase and reboot. Reproduce with:

```bash
python tools/verify_ble_frame.py
```

### 2.3 Correction to [RE]'s CRC parameters

[RE] used `crcbeagle` to fit the trailing checksum and published:

> `poly = 0x4C11DB7, reflect_in = True, reflect_out = True, init = 0x0, xor_out = 0xF43F44AC`
> …computed over the **whole** frame minus the checksum.

Those parameters reproduce **14 / 39** of the captured packets — specifically, only
the ones sharing the 4-byte prefix and length of the four alarm packets that were
fed to `crcbeagle`. The fit is degenerate: all four fitting samples had identical
`AA 10 00 57` prefixes and identical lengths, so the solver absorbed the constant
contribution of that prefix into `xor_out`. The failing frames are all off by a
single constant XOR (`0x9d4efac4` for the `AA 08 00 A8` family), which is the
signature of exactly that error.

The plain-CRC-32-over-`frame[4:]` reading is 39/39 and needs no magic constant.
**Use the standard CRC-32.** [HERE]

> This is a correction to a third-party writeup, not to the strap. If a future
> capture contradicts it, `tools/verify_ble_frame.py` is the place to add the
> packet and find out.

---

## 3. Payload structure — [RE] + [HERE]

The first payload byte (frame byte 4) is a **packet type**; the second is a
**rolling counter**.

| Type | Direction | Meaning | Evidence |
|------|-----------|---------|----------|
| `0x23` | → strap | command | [RE] all `CMD_TO_STRAP` writes |
| `0x28` | ← strap | live 1 Hz HR/RR record | [RE] "activity started" stream |
| `0x2f` | ← strap | history sample (96-byte) | [RE] sync burst |
| `0x30` | ← strap | event record | [RE] `EVENTS_FROM_STRAP` |
| `0x31` | ← strap | sync batch descriptor | [RE] end-of-burst packet |

### 3.1 Short command — 12-byte frame (`len = 8`)

```
AA 08 00 A8 | 23 | cc | KK | vv | crc32
             type  ctr  cmd  val
```

`cc` is a counter that increments per command. **[RE] states it is not validated by
the strap** ("This means that the packet count is not checked and used at all" —
after successfully replaying a stale-counter packet). We still increment it.

Command bytes `KK` observed by [RE]:

| `KK` | Meaning | Captured bytes |
|------|---------|----------------|
| `0x03` | start/stop activity or recording | `…8c 03 01…` start, `…8d 03 00…` stop |
| `0x0e` | heart-rate broadcast on/off | `…08 0e 01…` on, `…07 0e 00…` off |
| `0x16` | trigger data retrieval on `DATA_FROM_STRAP`; also sent when an alarm is tapped | `…0e 16 00…` |
| `0x1d` | reboot device | `…d4 1d 00…` |
| `0x45` | **labelled "Alarm off" by [RE]** — with value `0x01` | `…91 45 01…` |
| `0x23` | sent during sync | — |
| `0x24` | get a string from device | — |
| `0x4c` | get device name | — |
| `0x73`, `0x74` | "enable" pair sent at end of sync; ack `01` on `CMD_FROM_STRAP` | `…15 73 01…`, `…16 74 01…` |
| `0x43`, `0x75`, `0x76`, `0x14` | unknown | — |

### 3.2 Alarm set — 20-byte frame (`len = 16`)

The one command we most need. [RE]'s capture, re-segmented against the framing in §2:

```
AA 10 00 57 | 23 | cc | 42 | 01 | TTTTTTTT | 00 00 00 00 | crc32
             type  ctr  cmd sub   u32 LE      4 zero        LE
                                  unix time     bytes
```

Captured samples ([RE], alarm set from the official app):

| Frame | Decoded alarm time |
|---|---|
| `aa10005723 6d 4201 d0366566 00000000 f62deb81` | 07:00 |
| `aa10005723 6e 4201 0c376566 00000000 1023ccef` | 07:01 |
| `aa10005723 6f 4201 207d6566 00000000 fea1e060` | 12:00 |
| `aa10005723 70 4201 50116566 00000000 f226a8bd` | 04:20 |

- `0x42` = set-alarm command, `0x01` = subcommand/enable. Constant across all captures.
- The timestamp is **u32 little-endian unix seconds, absolute** — an alarm is set as
  a wall-clock instant, not an offset. This is what makes the countdown timer easy:
  a 20-minute nap timer is just `now + 1200`.
- The 4 zero bytes are constant in every capture. [RE] calls them padding. Never
  observed non-zero, so I send zeros; **what they mean is unverified.**
- [RE] confirms a **hand-built** alarm frame with a recomputed checksum was
  accepted by the strap and fired. That is the strongest evidence available that
  this layout is right and that the strap validates the CRC but not the counter.

> [RE] notes the same 20-byte shape is used for the sync request:
> `aa10005723 {ctr} 17 01 {batch_u32} 00000000 {crc32}` — command `0x17`, so the
> `KK SS u32 pad4` layout generalises.

### 3.3 What I will NOT build from guesses

| Feature | Status |
|---|---|
| **Cancel a pending alarm** | **UNVERIFIED — STUBBED.** [RE] labels `…91 45 01…` "Alarm off", but the value byte is `01` (on-looking), the command byte `0x45` is otherwise listed as "IDK", and there is no capture of it actually cancelling anything. We do not know whether cancel is `0x45`, or `0x42` with sub `0x00`, or an alarm set to a past/sentinel time. **The scheduler's `cancel` therefore drops the alarm from *our* schedule and tells you plainly that it could not be un-set on the strap.** |
| **Multiple simultaneous alarms** | Not supported by the hardware as far as [RE] can tell — the writeup's opening complaint is that "its alarm can only be set to ring only once a day". Our scheduler keeps a multi-alarm list but pushes only the next one to the strap. |
| **Arbitrary haptic buzz / vibration pattern** | **UNVERIFIED — STUBBED.** [NOOP] has "Breathe" and "Intervals" features that buzz the strap on demand, so a direct-haptic command certainly exists — but NOOP's BLE layer is unpublished and [RE] never captured one. We cannot synthesise it. A "buzz now" is therefore implemented as *set an alarm for `now + 2s`*, which is honest and works, but is a workaround, not the real command. |
| **Battery level** | **UNVERIFIED.** Not captured by [RE]. May be the standard Battery Service (`0x180F`/`0x2A19`); the code *tries* that and reports "unavailable" if absent, rather than inventing a custom read. |
| **The 65 trailing bytes of the 96-byte history packet** | [RE]: "I have no idea what it is". Untouched. |

---

## 4. Two hard constraints — both worse than they look

### 4.1 One host at a time (the user's stated constraint) — confirmed and sharpened

[NOOP]'s README, on WHOOP 5.0/MG:

> *"A WHOOP strap holds an encrypted Bluetooth **bond with only one device at a
> time**… If HR streams fine yet **buzz, alarm, double-tap and history don't work**,
> that's the tell: the strap isn't truly bonded to this device."*

So it is not merely that two hosts can't hold a *connection* — they can't hold the
**bond**, and the bond is what alarm/haptic requires. Consequences:

- Connect-send-disconnect (which is what we do) releases the *connection* but does
  **not** hand the bond back to NOOP or the official app. Re-pairing may be needed
  on the other side.
- Live HR over the standard profile keeps working for everyone regardless, which
  makes it a misleading health check — HR streaming is **not** evidence that an
  alarm will land.
- Therefore the backend verifies an alarm by the write succeeding on
  `CMD_TO_STRAP`, and the UI never claims "alarm set" on the strength of an HR
  reading.

### 4.2 Writing to `CMD_TO_STRAP` from Linux/Python is unreliable — [RE], and this is a real risk to the alarm feature

This is the single biggest threat to Feature 2, and it comes straight from the
source the design is based on. [RE], having built a correct alarm frame:

> *"And no go, sometimes it throws an error and sometimes it just doesn't… so let's
> send data from the BLE scanner [Android app] — **and it works**. So why it doesn't
> work with a computer, I don't know… I have tried to do it with another library
> (**bleak**), and it doesn't work, so the next try is with gatttool… and this
> 'works', it works **randomly** but it is good enough for me."*

The exact stack in the architecture spec — **`bleak`, from a computer** — is the one
[RE] reports failing. The likely cause is the bond/encryption requirement (a
BlueZ-level pairing issue, not a packet issue), but that is my inference, not
established.

**How this is handled rather than ignored:**

1. The packet builders are pure functions with no I/O, fully unit-tested against
   [RE]'s captured frames. Whatever happens at the transport layer, *the bytes are
   right* — proven, not hoped.
2. The transport is behind an interface with more than one backend, so if `bleak`
   proves flaky on your machine a `gatttool`/`bluetoothctl` shell backend can be
   swapped in without touching the scheduler or the API.
3. Every alarm write is **verified and retried**, and a failure is surfaced as a
   loud, specific UI error — never a silent success. This is exactly the
   "I need to know if an alarm did NOT get set" requirement, and it is not
   decoration: on this hardware, failure is the expected case often enough to
   design around.
4. Phase 4 begins with a **bench test against your actual strap** before any
   scheduler work, because if writes don't land, the scheduling layer is moot.

### 4.3 Hardware generation

Everything in [RE] is from a **WHOOP 4.0**. [NOOP] states that on 5.0/MG only live
HR is confirmed working, and that deeper protocol support "is still being reverse
engineered". If your strap is a 5.0 or MG, **assume the alarm frame in §3.2 is
untested on your hardware** until it fires once.

---

## 5. Connection lifecycle we implement (per the hard constraint)

```
idle ──scan──▶ connecting ──▶ connected ──▶ writing ──▶ disconnecting ──▶ idle
                    │                                          ▲
                    └────────── error (claimed/timeout) ────────┘
```

- No connection is held while idle. Ever.
- A single asyncio lock serialises strap access inside our process, so two API
  calls can never race for the radio.
- Connect attempts have a hard timeout; on failure the state machine returns to
  `idle` and the API returns a human-readable reason, distinguishing at minimum:
  *strap not found* / *already claimed by another app* / *connected but write
  rejected* / *timed out*.
- The current state is exposed at `GET /api/strap/state` and rendered as a
  persistent banner in the UI.
