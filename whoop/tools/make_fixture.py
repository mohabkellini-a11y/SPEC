#!/usr/bin/env python3
"""Build a SYNTHETIC NOOP-shaped SQLite database for development and tests.

THIS IS NOT REAL DATA AND IT IS NOT NOOP'S REAL SCHEMA.

It is a best-effort reconstruction of the record shapes recovered from
StrandAnalytics (see SCHEMA_NOTES.md 1), used for two things:

  1. so the dashboard can be run and tested without a real NOOP install,
  2. so the adapter's runtime schema resolution has something to resolve.

The numbers are generated from a seeded RNG. Do not read anything into them.
When you point the app at your real NOOP database, expect the resolver to find
different table and column names — that is the whole reason it resolves at
runtime instead of hardcoding.

    python tools/make_fixture.py [--out data/fixture_noop.sqlite3] [--days 120]
                                 [--camel] [--cold-start]
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def camelise(name: str) -> str:
    head, *rest = name.split("_")
    return head + "".join(w.capitalize() for w in rest)


def build(out: Path, days: int, camel: bool, cold_start: bool, seed: int = 20260728) -> None:
    rng = random.Random(seed)
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()

    def col(name: str) -> str:
        return camelise(name) if camel else name

    conn = sqlite3.connect(out)
    conn.executescript(
        f"""
        CREATE TABLE daily_metrics (
            {col('day')}             TEXT PRIMARY KEY,
            {col('total_sleep_min')} REAL,
            {col('efficiency')}      REAL,
            {col('deep_min')}        REAL,
            {col('rem_min')}         REAL,
            {col('light_min')}       REAL,
            {col('disturbances')}    INTEGER,
            {col('resting_hr')}      INTEGER,
            {col('avg_hrv')}         REAL,
            {col('recovery')}        REAL,
            {col('strain')}          REAL,
            {col('exercise_count')}  INTEGER NOT NULL DEFAULT 0,
            {col('spo2_pct')}        REAL,
            {col('skin_temp_dev_c')} REAL,
            {col('resp_rate_bpm')}   REAL,
            {col('steps')}           INTEGER,
            {col('active_kcal_est')} REAL,
            {col('sleep_start')}     INTEGER,
            {col('sleep_end')}       INTEGER
        );
        CREATE TABLE sleep_sessions (
            {col('start_ts')}   INTEGER NOT NULL,
            {col('end_ts')}     INTEGER NOT NULL,
            {col('efficiency')} REAL,
            {col('resting_hr')} INTEGER,
            {col('avg_hrv')}    REAL,
            {col('stages_json')} TEXT
        );
        CREATE TABLE hr_samples (
            {col('ts')}  INTEGER NOT NULL,
            {col('bpm')} INTEGER NOT NULL
        );
        CREATE INDEX idx_hr_ts ON hr_samples ({col('ts')});
        CREATE TABLE grdb_migrations (identifier TEXT PRIMARY KEY);
        """
    )
    conn.execute("INSERT INTO grdb_migrations VALUES ('v1_initial'), ('v2_skin_temp')")

    today = datetime.now(tz=timezone.utc).date()
    start_day = today - timedelta(days=days - 1)

    # Personal "true" values the synthetic series wobbles around.
    hrv_base, rhr_base, resp_base = 62.0, 52.0, 14.6

    daily_rows, sleep_rows, hr_rows = [], [], []

    for i in range(days):
        d: date = start_day + timedelta(days=i)
        day_key = d.isoformat()
        seasonal = math.sin(i / 11.0)

        # --- sleep -----------------------------------------------------
        bed_hour = 23 + rng.uniform(-1.2, 1.2)
        sleep_start = int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp()
                          - 86400 + bed_hour * 3600)
        in_bed_min = rng.gauss(465, 48)
        in_bed_min = max(240.0, min(600.0, in_bed_min))
        sleep_end = int(sleep_start + in_bed_min * 60)

        efficiency = max(0.60, min(0.98, rng.gauss(0.88, 0.05)))
        tst = in_bed_min * efficiency
        deep = tst * rng.uniform(0.14, 0.24)
        rem = tst * rng.uniform(0.18, 0.28)
        light = tst - deep - rem
        disturbances = rng.randint(0, 9)

        hrv = max(12.0, rng.gauss(hrv_base + 7.0 * seasonal, 7.5))
        rhr = max(35.0, rng.gauss(rhr_base - 2.0 * seasonal, 2.6))
        resp = max(8.0, rng.gauss(resp_base, 0.7))

        strain = max(0.0, min(21.0, rng.gauss(11.5, 3.6)))

        # Recovery is nil for the first 4 nights (RecoveryScorer cold-start gate).
        if cold_start and i >= days - 3:
            recovery = None
        elif i < 4:
            recovery = None
        else:
            z = (0.60 * (hrv - hrv_base) / 9.4
                 + 0.20 * (rhr_base - rhr) / 3.3
                 + 0.15 * (efficiency - 0.85) / 0.12)
            recovery = round(100.0 / (1.0 + math.exp(-1.6 * (z + 0.20))), 1)

        stages = _stage_segments(rng, sleep_start, sleep_end, efficiency)

        daily_rows.append((
            day_key, round(tst, 1), round(efficiency, 4), round(deep, 1), round(rem, 1),
            round(light, 1), disturbances, int(round(rhr)), round(hrv, 1), recovery,
            round(strain, 2), rng.choice([0, 0, 1, 1, 2]),
            round(rng.gauss(96.5, 1.0), 1), round(rng.gauss(0.0, 0.35), 2),
            round(resp, 2), rng.randint(2200, 15400), round(rng.gauss(620, 190), 1),
            sleep_start, sleep_end,
        ))
        sleep_rows.append((sleep_start, sleep_end, round(efficiency, 4),
                           int(round(rhr)), round(hrv, 1), json.dumps(stages)))

        # --- HR samples: only the last 3 days, 1 per 60 s -------------
        if i >= days - 3:
            t = int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp())
            now_ts = int(datetime.now(tz=timezone.utc).timestamp())
            for minute in range(0, 1440):
                ts = t + minute * 60
                if ts > now_ts:
                    break
                hour = (minute / 60.0)
                if hour < 7:
                    bpm = rng.gauss(rhr + 3, 3)
                elif 17.5 <= hour < 18.4:          # a plausible workout block
                    bpm = rng.gauss(148, 11)
                else:
                    bpm = rng.gauss(72, 8) + 6 * math.sin(hour / 3.0)
                hr_rows.append((ts, int(max(35, min(198, bpm)))))

    conn.executemany(
        f"INSERT INTO daily_metrics VALUES ({','.join('?' * 19)})", daily_rows)
    conn.executemany(
        f"INSERT INTO sleep_sessions VALUES ({','.join('?' * 6)})", sleep_rows)
    conn.executemany("INSERT INTO hr_samples VALUES (?, ?)", hr_rows)
    conn.commit()
    conn.close()

    print(f"Wrote {out}")
    print(f"  {len(daily_rows)} daily rows  ({daily_rows[0][0]} .. {daily_rows[-1][0]})")
    print(f"  {len(sleep_rows)} sleep sessions")
    print(f"  {len(hr_rows)} heart-rate samples")
    print(f"  column style: {'camelCase' if camel else 'snake_case'}")
    print("  NOTE: synthetic data, reconstructed schema. Not NOOP's real schema.")


def _stage_segments(rng: random.Random, start: int, end: int, efficiency: float) -> list[dict]:
    """Build a plausible hypnogram in NOOP's exact StageSegment shape."""
    segments: list[dict] = []
    t = start
    # NOOP's stage strings, verbatim: wake | light | deep | rem
    cycle = ["light", "deep", "light", "rem"]
    idx = 0
    while t < end:
        stage = cycle[idx % len(cycle)]
        if rng.random() > efficiency:
            stage = "wake"
        dur = rng.randint(12, 34) * 60
        seg_end = min(t + dur, end)
        if seg_end > t:
            segments.append({"start": t, "end": seg_end, "stage": stage})
        t = seg_end
        idx += 1
    return segments


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, default=REPO_ROOT / "data" / "fixture_noop.sqlite3")
    parser.add_argument("--days", type=int, default=120)
    parser.add_argument("--camel", action="store_true",
                        help="use camelCase columns, to exercise the resolver")
    parser.add_argument("--cold-start", action="store_true",
                        help="leave recent recovery NULL, as during calibration")
    args = parser.parse_args()
    build(args.out, args.days, args.camel, args.cold_start)


if __name__ == "__main__":
    main()
