# Strap — a local-first dashboard for a WHOOP strap you own

A personal interoperability project. Reads biometrics from
[NOOP](https://github.com/noop-app/noop)'s local SQLite database (read-only) and
serves a mobile-first dashboard on your LAN. No cloud, no accounts, no WHOOP
servers, no telemetry, no CDN — every asset is vendored.

**Not a medical device.** Every number here is an approximation computed on your
own machine from published methods. Not clinically validated, not medical advice,
and not WHOOP's proprietary scores.

---

## Status: Phase 3 of 5

| Phase | Scope | State |
|-------|-------|-------|
| 0 | Schema + BLE investigation | **done** — `SCHEMA_NOTES.md`, `BLE_NOTES.md` |
| 1 | Read-only dashboard, today's metrics | **done** |
| 2 | Trends + 7/30/90-day charts | **done** |
| 3 | Habit journal + workout log | **done** |
| 4 | BLE alarm + countdown timer | **bench test ready** — needs your strap |
| 5 | PWA packaging, correlations, export | not started |

Endpoints for unbuilt phases are absent rather than stubbed with fake data. The
one exception is `GET /api/strap/state`, which exists so the UI can display the
truth — that nothing is connected to the strap yet.

The correlations view is Phase 5, because it needs the habit journal from
Phase 3 to correlate anything against.

---

## Read this before you start

Two findings from Phase 0 change what is realistic. Both are documented in full
in the notes files; the short version:

1. **NOOP's database schema is not public.** The `noop-app/noop` repository ships
   two Swift packages (design + analytics); the app, its BLE layer and its
   `WhoopStore` persistence layer are not in it. There is no `CREATE TABLE`
   anywhere to read. So this project **discovers the schema at runtime** instead
   of hardcoding it — see "First run" below. Expect to run the probe once.

2. **The alarm feature (Phase 4) is at genuine risk, and not because of the
   packet format.** The packet format is solved and verified. The risk is the
   transport: the community researcher whose captures this is built on reports
   that writing to the strap's command characteristic **from a computer via
   `bleak` did not work**, and via `gatttool` "works randomly". The strap also
   holds an encrypted BLE bond with exactly one host at a time, and haptics
   require that bond — so NOOP and this dashboard genuinely cannot both have it.
   See `BLE_NOTES.md` §4. Phase 4 will therefore start with a bench test against
   your actual strap before any scheduler is written.

**Pinned NOOP version:** commit `d97fb89216baf6e51e654787cea1d64decd22afe`
(tag `noop`, 2026-06-11). App release line at the time of investigation: v1.8.8.

---

## Setup

Requires Python 3.11+.

```bash
cd whoop
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env
```

### Find your NOOP database

```bash
python -m app.probe --find
```

This searches the usual per-platform application-support directories for
NOOP-shaped SQLite files and tells you which ones actually resolve. Put the
winner in `.env`:

```ini
NOOP_DB_PATH=/Users/you/Library/Application Support/NOOP/noop.sqlite3
PORT=8765
DISPLAY_TZ=Europe/London
```

### Check what the adapter can see

```bash
python -m app.probe
```

Prints every table and column in your database, exactly which ones it resolved
to which canonical field, what it could not resolve, and a sample row. **Run this
first.** It is the difference between "the tile is empty because there is no
data" and "the tile is empty because the column is called something else".

Add `--sql` to dump the real `CREATE TABLE` statements — if you paste those into
an issue, the resolver can be taught your schema permanently.

### Run

```bash
python -m app.main
# or: uvicorn app.main:app --host 0.0.0.0 --port 8765
```

Open `http://localhost:8765`, or `http://<your-machine-ip>:8765` from your phone
on the same network.

> The server binds `0.0.0.0` so your phone can reach it. **There is no
> authentication.** Anyone on your LAN can read your biometrics. Keep it on a
> network you trust; set `HOST=127.0.0.1` if you only want local access.

### No NOOP install yet?

There is a synthetic fixture so you can see the thing work:

```bash
python tools/make_fixture.py
# then set NOOP_DB_PATH=./data/fixture_noop.sqlite3
```

The data is generated from a seeded RNG and the schema is a *reconstruction* of
the record shapes recovered from NOOP's analytics package — **not** NOOP's real
schema. Do not read anything into the numbers.

---

## When the schema does not resolve

The probe will say so, and `/api/today` returns a 503 listing the tables it found
rather than silently showing zeros. To fix it, create `schema_map.json` next to
`.env`:

```json
{
  "daily": {
    "table": "DailyMetricRecord",
    "columns": { "avg_hrv": "hrvRmssdMs", "recovery": "recoveryScore" }
  },
  "sleep": { "table": "SleepSessionCache" },
  "hr":    { "table": "hr_stream", "columns": { "bpm": "value" } }
}
```

You only need to pin what the resolver got wrong; anything omitted is still
auto-detected. Bad pins are reported in the probe output and in
`/api/diagnostics` — they are never silently ignored.

Canonical field names are the keys of `DAILY_FIELDS`, `SLEEP_FIELDS` and
`HR_FIELDS` in `app/noop_adapter.py`.

---

## Design notes

### One file knows the schema
`app/noop_adapter.py` is the only module that touches NOOP's database or knows
what its tables are called. Everything above it speaks canonical field names. A
NOOP schema change breaks there and nowhere else.

### Reads cannot write
The connection is opened with a `file:...?mode=ro` URI, `PRAGMA query_only=ON` is
set as a second barrier, and connections are per-request and short-lived so no
handle is held on a file NOOP owns. There is a test that asserts `DELETE` and
`CREATE TABLE` both raise, and one that asserts the file's mtime and size are
unchanged after a read cycle.

### Nothing is invented
- A metric with no value says *why*: "no data", "calibrating", "not in this NOOP
  DB", or "not stored by NOOP" are four different states and the UI shows which.
- Recovery is `NULL` during NOOP's 4-night cold start (its `RecoveryScorer`
  returns nil rather than guessing). The dashboard shows "Calibrating", not 0%.
- Values outside the plausibility gates NOOP's own baseline model uses are
  flagged as suspect rather than displayed as fact.
- Strap battery is very likely **not stored** by NOOP at all — it is not a field
  on its daily record. The tile says so instead of showing 0%.

### Gaps stay gaps
A day the strap was not worn is a hole in the chart, not an interpolated point.
Series are densified against a full calendar range so a missing row becomes an
explicit `null`; the rolling mean counts *days*, not readings, so a stale value
cannot drift forward across a gap; and every chart shows a coverage bar reading
"36 of 90 days" so a 90-day average over 36 nights cannot pass for a full one.
A period-over-period delta that is too sparse to mean anything renders as
"not enough data to compare" instead of a number.

### Your data lives in a different file from NOOP's
NOOP's database is opened read-only and never written to. Everything you type —
habits, journal entries, notes, workouts — goes in `APP_DB_PATH`, a separate
SQLite file this app owns, migrated in place via `PRAGMA user_version`. A NOOP
update, reinstall or schema change cannot touch it. The two are joined only by
the day key, and the journal keeps working when NOOP's file is missing entirely.

### Not logged is not zero
A habit has three states, not two: unset, yes, and an explicit no. "I did not
drink" and "I did not log" are different facts, and the correlations in Phase 5
must not read silence as a zero. In the UI: one tap for yes, two for an explicit
no, three to clear.

### Workout suggestions are suggestions
The detector reuses NOOP's own constants (`minExerciseMin`, `hrMarginBPM`,
`mergeGapS`, `restingPercentile`) but **not** its motion channel, which the
adapter cannot resolve. Heart rate alone, `resting + 15 bpm` fires on ordinary
waking life — measured against a real day it flagged 17 "workouts" in 24 hours.
Two extra gates stand in for the missing motion check: a bout must sit above the
day's *median* HR, and must average 30 bpm above resting.

The honest cost: **easy sessions will not be suggested.** A flat walk or gentle
yoga is indistinguishable from sitting at a desk when all you have is heart rate.
Log those by hand. Nothing is ever written to your log without you confirming it,
and dismissals are remembered by a key that survives re-detection.

### Every metric is labelled
`app/metrics_meta.py` carries the label, unit, computation method and
approximation status for every displayed value. The UI renders tiles *from* that
table, so a tile cannot exist without declaring what it is. Tap any tile for the
method and its caveats.

---

## API

| Endpoint | Purpose |
|---|---|
| `GET /api/health` | Liveness; whether the NOOP DB is reachable and resolved |
| `GET /api/diagnostics` | Full schema-resolution report |
| `GET /api/metrics/meta` | Label/unit/method/approximation for every metric |
| `GET /api/today?day=YYYY-MM-DD&fallback=` | Today's (or a given day's) metrics |
| `GET /api/heart-rate?day=&max_points=` | Decimated HR samples for a day |
| `GET /api/trends?days=&metrics=&end=&rolling=` | Daily series, rolling mean, period delta |
| `GET/POST /api/habits`, `PATCH/DELETE /api/habits/{id}` | Define your own habits |
| `GET/PUT /api/journal/{day}` | One day's habits and note |
| `GET /api/journal?days=&end=` | Journal over a window |
| `GET/POST /api/workouts`, `PATCH/DELETE /api/workouts/{id}` | Workout log |
| `GET /api/workouts/suggestions?days=` | Elevated-HR blocks awaiting confirmation |
| `POST /api/workouts/suggestions/{key}/confirm\|dismiss` | Accept or reject one |
| `GET /api/workouts/summary?days=` | Weekly volume + strain-vs-recovery scatter |
| `GET /api/store/stats` | What lives in this app's own database |
| `GET /api/strap/state` | BLE connection state (Phase 1: always idle) |
| `GET /api/docs` | OpenAPI browser |

---

## Tests

```bash
python -m pytest tests/ -q      # 469 tests
python tools/verify_ble_frame.py
```

- `tests/test_ble_frame.py` — the frame format against 39 captured packets:
  length field, header CRC-8, trailing CRC-32, alarm and command payload layout.
- `tests/test_ble_packets.py` — every command builder rebuilds a captured frame
  byte for byte, and the unverified commands raise instead of guessing.
- `tests/test_noop_adapter.py` — schema resolution (snake_case *and* camelCase),
  read-only enforcement, overrides, stage decoding, cold-start nulls.
- `tests/test_analytics.py` — rolling means, period deltas, slopes and coverage
  on hand-checkable inputs. Mostly about gaps not silently becoming numbers.
- `tests/test_store.py` — migrations, value validation, and the unset-vs-zero
  distinction; also asserts writing the journal never touches NOOP's file.
- `tests/test_workout_detect.py` — synthetic HR days with known answers, including
  the false-positive case the extra gates exist to reject and the low-intensity
  case they knowingly miss.
- `tests/test_api.py`, `tests/test_api_journal.py` — the honest-empty-state
  contract and every failure mode.

To see the gap handling for yourself, build a fixture and delete some days:

```bash
python tools/make_fixture.py --days 60 --out data/sparse.sqlite3
sqlite3 data/sparse.sqlite3 "DELETE FROM daily_metrics WHERE day BETWEEN '2026-06-20' AND '2026-06-29'"
```

The charts break the line across the hole and the coverage bar turns amber.

---

## Layout

```
app/
  config.py         .env loading
  noop_adapter.py   THE schema boundary — NOOP, read-only, runtime-resolved
  store.py          THIS app's own database — journal, workouts, habits
  analytics.py      descriptive stats over daily series (pure functions)
  workout_detect.py elevated-HR bout suggestions (pure functions)
  metrics_meta.py   label/unit/method/approximation for every metric
  routes_journal.py journal / habit / workout endpoints
  probe.py          schema discovery CLI
  main.py           FastAPI
web/                vendored SPA (no build step, no external requests)
  chart.js          SVG line, bar and scatter; nulls break the path, never bridged
  journal.js        fast daily entry
  workouts.js       suggestions, manual entry, history
app/ble/
  packets.py        WHOOP frame codec + command builders (pure, no radio)
tools/
  verify_ble_frame.py   reproducible BLE frame verification
  bench_strap.py        Phase 4 hardware bench test — see docs/PHASE4_BENCH.md
  make_fixture.py       synthetic NOOP-shaped database
tests/
SCHEMA_NOTES.md     what NOOP's data model is, and what could not be verified
BLE_NOTES.md        GATT UUIDs, frame format, and what is deliberately stubbed
```

---

## Legal

Independent, unofficial, non-commercial. Not affiliated with, endorsed by, or
connected to WHOOP, Inc. — "WHOOP" is used nominatively to identify the hardware.
Use only with a device you own. No login, paywall or DRM is bypassed; this reads
data you generated, from a database on your own machine.
