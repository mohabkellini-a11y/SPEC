# SCHEMA_NOTES.md — NOOP's local SQLite store

Investigation output for Step 0.1. Written 2026-07-28.

**Source pinned:** `github.com/noop-app/noop` @ `d97fb89216baf6e51e654787cea1d64decd22afe`
(tag `noop`, committed 2026-06-11). App release line at time of writing: **v1.8.8**
(from the repo README download table — release artifacts are not in git).

---

## 0. The headline finding: the schema is not in the public repo

**The public `noop-app/noop` repository does not contain the app.** It contains
exactly two Swift packages:

```
Packages/StrandDesign/      SwiftUI components (rings, charts, palette)
Packages/StrandAnalytics/   the on-device math (HRV, recovery, strain, sleep, workouts)
```

`StrandAnalytics/Package.swift` declares two dependencies by **local path**:

```swift
dependencies: [
    .package(path: "../WhoopProtocol"),   // BLE frame decode  — NOT PUBLISHED
    .package(path: "../WhoopStore"),      // SQLite/GRDB layer — NOT PUBLISHED
]
```

Neither `../WhoopProtocol` nor `../WhoopStore` exists in the repo. Nor does the
`Strand/` app target that the README references (`Strand/Data/MetricCatalog.swift`,
`Strand/BLE`, `Strand/Collect`). A `grep -ril "sqlite\|CREATE TABLE\|GRDB"` across
all Swift sources returns **zero** hits; the only mentions are in `README.md` and in
`Package.resolved`.

So there is **no `CREATE TABLE` statement anywhere I can read**, and I am not going
to invent one.

What I *can* state with confidence:

- The persistence layer is **GRDB.swift 6.29.3** (pinned in
  `Packages/StrandAnalytics/Package.resolved`), i.e. plain SQLite with GRDB record
  types mapped onto tables.
- Column naming is **snake_case**. Direct evidence — `AnalyticsEngine.swift:276`:
  ```swift
  _ = sleepStart; _ = sleepEnd  // available for callers wiring sleep_start/end columns
  ```
  Swift properties are camelCase, the columns they map to are `sleep_start` /
  `sleep_end`. GRDB's default is a literal property-name match, so NOOP must be
  declaring an explicit `CodingKeys`/column mapping — meaning **camelCase column
  names are also possible in tables I haven't seen**. The adapter probes for both.

### What this means for this project

The read-only adapter (`app/noop_adapter.py`) **introspects the real database at
runtime** instead of hardcoding a schema, and resolves logical fields against
candidate column names. Run:

```bash
python -m app.probe            # dumps the real sqlite_master + resolution report
```

against your actual NOOP DB and it will print exactly what it found and what it
failed to map. Anything unresolved can be pinned by hand in `schema_map.json`.
This is the "breaks in one file" requirement taken seriously: the schema is a
runtime input, not a compile-time assumption.

---

## 1. The logical model (recovered — high confidence)

`StrandAnalytics` is a *pure* package ("no DB access") but it **constructs the
WhoopStore record types**, so their exact field names, optionality and units are
readable. These records are what the tables hold.

### 1.1 `DailyMetric` — one row per calendar day

Constructed at `AnalyticsEngine.swift:258-275`. Comment at line 40 calls it
"DailyMetric in the WhoopStore cache shape".

| Swift field     | Likely column      | Type      | Unit / range | Notes |
|-----------------|--------------------|-----------|--------------|-------|
| `day`           | `day`              | TEXT      | `YYYY-MM-DD` | **UTC** calendar day (`AnalyticsEngine.isoDay`, `TimeZone(identifier: "UTC")`) |
| `totalSleepMin` | `total_sleep_min`  | REAL?     | minutes      | AASM TST, in-bed weighted |
| `efficiency`    | `efficiency`       | REAL?     | **0..1**, not % | TST/TIB |
| `deepMin`       | `deep_min`         | REAL?     | minutes      | approximate staging |
| `remMin`        | `rem_min`          | REAL?     | minutes      | approximate staging |
| `lightMin`      | `light_min`        | REAL?     | minutes      | approximate staging |
| `disturbances`  | `disturbances`     | INTEGER?  | count        | |
| `restingHr`     | `resting_hr`       | INTEGER?  | bpm          | lowest 5-min rolling mean in-bed |
| `avgHrv`        | `avg_hrv`          | REAL?     | **ms (RMSSD)** | mean of 5-min-window RMSSD |
| `recovery`      | `recovery`         | REAL?     | 0..100       | `nil` during cold start — see §2 |
| `strain`        | `strain`           | REAL?     | 0..21        | |
| `exerciseCount` | `exercise_count`   | INTEGER   | count        | non-optional |
| `spo2Pct`       | `spo2_pct`         | REAL?     | %            | **always `nil` from analytics** — only ever populated by strap decode or CSV import |
| `skinTempDevC`  | `skin_temp_dev_c`  | REAL?     | °C *deviation* | delta vs personal baseline, **not** absolute temp; rounded to 2dp |
| `respRateBpm`   | `resp_rate_bpm`    | REAL?     | breaths/min  | |
| `steps`         | `steps`            | INTEGER?  | count        | |
| `activeKcalEst` | `active_kcal_est`  | REAL?     | kcal         | estimate |
| *(implied)*     | `sleep_start`      | INTEGER?  | unix seconds | per the `:276` comment; not set by `analyzeDay` |
| *(implied)*     | `sleep_end`        | INTEGER?  | unix seconds | ditto |

> `spo2Pct: nil` is hardcoded in the constructor call. If your dashboard shows an
> empty SpO₂ it may be genuinely absent rather than a mapping failure.

### 1.2 `CachedSleepSession` — one row per detected sleep session

Constructed at `AnalyticsEngine.swift:280-286`.

| Swift field   | Likely column | Type    | Unit |
|---------------|---------------|---------|------|
| `startTs`     | `start_ts`    | INTEGER | unix seconds |
| `endTs`       | `end_ts`      | INTEGER | unix seconds |
| `efficiency`  | `efficiency`  | REAL    | 0..1 |
| `restingHr`   | `resting_hr`  | INTEGER? | bpm |
| `avgHrv`      | `avg_hrv`     | REAL?   | ms |
| `stagesJSON`  | `stages_json` | TEXT?   | JSON array, see below |

`stages_json` is a JSON encoding of `[StageSegment]` (`SleepStager.swift:33`):

```json
[{"start": 1750000000, "end": 1750001800, "stage": "light"}, ...]
```

`stage` ∈ `"wake" | "light" | "deep" | "rem"` (exact strings, lowercase).
`start`/`end` are **wall-clock unix seconds**.

### 1.3 Raw sample streams (`WhoopProtocol` types)

Field names are visible from constructor call sites in `StrandAnalyticsTests`:

| Type             | Fields                          | Units |
|------------------|---------------------------------|-------|
| `HRSample`       | `ts: Int`, `bpm: Int`           | unix seconds, bpm |
| `RRInterval`     | `ts`, interval                  | unix seconds, **ms** (`HRVAnalyzer` gates on 300–2000 ms) |
| `RespSample`     | `ts`, rate                      | unix seconds, breaths/min |
| `GravitySample`  | `ts: Int`, `x`, `y`, `z: Double`| unix seconds, **g** (still threshold 0.01 g) |
| `StepSample`     | `ts`, count                     | unix seconds |
| `SkinTempSample` | `ts`, °C                        | unix seconds, °C absolute |

I did **not** see the RR/resp/step/skin-temp field *names* directly (the tests
construct them via helpers I can't see the signature of), only `HRSample(ts:bpm:)`
and `GravitySample(ts:x:y:z:)` verbatim. Treat the rest as inferred.

### 1.4 Timestamp conventions — verified

- All sample and session timestamps are **integer unix seconds**, not ms, not
  ISO strings, not Core Data reference dates.
- `DailyMetric.day` is a **UTC** `YYYY-MM-DD` string.
- **A sleep session is attributed to the day its `end` falls on** — `AnalyticsEngine`
  line ~137: `let matched = allSessions.filter { dayString($0.end) == day }`. A night
  from 23:00 Mon to 07:00 Tue belongs to **Tuesday**. This matters for the
  correlations view: "alcohol on day D vs HRV on day D+1" must respect it.
- The UTC day boundary means a late-evening workout can land on "tomorrow" for
  users west of UTC. NOOP passes a `tzOffsetSeconds` into the sleep detector for a
  daytime-nap guard, but the *day bucketing itself is UTC*. Our dashboard exposes
  the raw day key and does not silently re-bucket.

---

## 2. Recovery / strain / HRV / sleep: stored or computed?

**Computed on-device by NOOP, then cached into `DailyMetric`.** They are not
values the strap reports. Concretely, from the pinned source:

### Recovery — `RecoveryScorer.swift`
Weighted robust-z composite squashed through a logistic:

```
z    = Σ(wᵢ · zᵢ) / Σwᵢ     over available terms
score = 100 / (1 + exp(-K · (z - Z0)))
```
- weights: HRV **0.60**, resting HR **0.20**, respiration **0.05**, sleep performance **0.15**
- `K = 1.6`, `Z0 = -0.20` (chosen so z=0 → ~58%, WHOOP's published population mean)
- robust z: `(value − baseline) / max(1.253 × spread, 1e-9)`
- RHR and resp are *inverted* (lower is better)
- sleep term needs no baseline: `(efficiency − 0.85) / 0.12`
- bands: red < 34, yellow < 67, green ≥ 67
- **Cold start: returns `nil`** if the HRV baseline has < 4 valid nights. NOOP shows
  "Calibrating — N of 4 nights" instead. **Our dashboard must reproduce that state
  rather than rendering 0% or a blank tile.**

### Baselines — `Baselines.swift`
Winsorized EWMA, not a plain moving average:
- winsor clamp ±3×spread, hard-reject beyond 5×spread
- `spread` is an EWMA of absolute deviation; **×1.253 ≈ Gaussian σ**
- half-life 14 nights (center), 21 nights (spread)
- status ladder: `calibrating` (<4 valid nights) → `provisional` (4–13) → `trusted` (≥14); `stale` after 14 nights with no update
- plausibility gates (a value outside these is *rejected*, not clamped):
  | metric | min | max | floor spread |
  |---|---|---|---|
  | `hrv` | 5 | 250 ms | 5 |
  | `resting_hr` | 30 | 120 bpm | 2 |
  | `resp` | 4 | 40 /min | 0.5 |
  | `skin_temp` | 20 | 42 °C | 0.3 |

  These double as **sanity bounds for our own display** — anything outside them in
  the DB is suspect.

### HRV — `HRVAnalyzer.swift`
RMSSD and SDNN per Task Force (1996), from RR intervals:
- range filter 300–2000 ms
- Malik-style ectopic rejection: drop beats >20% from a 5-beat local median
- needs ≥20 clean intervals or returns `nil`
- **`avg_hrv` is RMSSD in ms**, averaged over 5-minute windows across the session

### Strain — `StrainScorer.swift`
Edwards/Banister TRIMP → logarithmic 0–21 scale. Tanaka HRmax from age unless
overridden.

### Sleep — `SleepStager.swift` (1084 lines, the biggest analyzer)
Gravity-spine stillness detection (0.01 g threshold, 15-min window, 70% still
fraction), HR-confirmed (mean HR ≤ 1.05 × baseline), then 4-class staging.
Minimum session 60 min; daytime naps (local 11:00–20:00) held to a stricter bar
(≥90 min **and** resting HR ≤ 0.95 × baseline).
The source labels staging **"APPROXIMATE"** in its own doc comment.

> Every one of these is explicitly documented by NOOP as an approximation of, not a
> reproduction of, WHOOP's proprietary model. Our UI carries that label through.

---

## 3. Where the database file lives

Not determinable from the repo (the app target isn't published). NOOP ships for
macOS, Windows and Linux, so the path is platform-dependent. **Set it explicitly**
via `NOOP_DB_PATH` in `.env`. Likely locations to check:

- macOS: `~/Library/Application Support/NOOP/` or `~/Library/Containers/…/Data/Library/Application Support/`
- Linux: `~/.local/share/NOOP/`
- Windows: `%APPDATA%\NOOP\`

`app/probe.py --find` will search those roots for `*.sqlite*` / `*.db` files and
report which ones look like a NOOP store.

---

## 4. Concurrency: reading a live SQLite file safely

NOOP will be writing while we read. Our adapter therefore:

1. opens `file:<path>?mode=ro` (URI mode, read-only — **cannot** create or write),
2. issues `PRAGMA query_only = ON` as a second barrier,
3. **never** issues `PRAGMA journal_mode` (that would attempt a write and fail — or
   worse, succeed and change NOOP's journaling),
4. opens a fresh connection per request and closes it (no long-lived handle on a
   file another process owns),
5. tolerates `SQLITE_BUSY` with a short `busy_timeout` rather than erroring out.

If NOOP is in WAL mode, a read-only connection still needs to read the `-wal` and
`-shm` sidecars; those must be readable too. If the DB is on a read-only mount and
in WAL mode, reads can fail — surfaced as a clear error, not a stack trace.

---

## 5. Open questions — explicitly unverified

| # | Question | Status |
|---|---|---|
| 1 | Actual table names (`daily_metrics`? `daily_metric`? `DailyMetric`?) | **unverified** — resolved at runtime by probe |
| 2 | Whether columns are snake_case or camelCase | **partially** — `sleep_start`/`sleep_end` are snake_case; adapter probes both |
| 3 | Raw HR sample table name and column names | **unverified** — needed for Phase 4 workout auto-detect |
| 4 | Whether battery level is persisted at all, or only live over BLE | **unverified** — README lists battery as a decoded stream, but no `DailyMetric` field holds it |
| 5 | Schema version / migration table | **unverified** — GRDB convention is `grdb_migrations`; probe reports it if present |
| 6 | Whether imported (WHOOP CSV / Apple Health) rows are distinguishable from strap-derived rows | **unverified** — matters for honest provenance labelling |

Items 3 and 4 are the ones that will bite: **strap battery is on the Phase 1
dashboard spec and may simply not exist in the DB.** If the probe finds no battery
column, the tile renders "unavailable — not stored by NOOP" rather than a zero.

---

## 6. Method note

Everything above is from reading source at the pinned commit, plus the repo README.
I did not run NOOP, did not have a real NOOP database to inspect, and did not
download a release binary (GitHub API access in this environment is scoped to this
repository only). Nothing here is inferred from WHOOP's official app or docs.
