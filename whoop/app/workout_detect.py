"""Suggest workouts from elevated-HR blocks in NOOP's heart-rate stream.

This is a SUGGESTION engine, not a detector of record. Nothing it produces
enters the log until you confirm it; dismissals are remembered.

Method, and where the numbers come from
---------------------------------------
The constants are taken from NOOP's own `WorkoutDetector` at the pinned commit,
so a suggestion here is at least calibrated the same way as the exercise count
NOOP shows:

    minExerciseMin    = 5.0    minimum bout length
    hrMarginBPM       = 15.0   how far above resting counts as "elevated"
    mergeGapS         = 150.0  gap below which two bouts are one bout
    restingPercentile = 10.0   percentile of the day's HR taken as resting

IMPORTANT DIFFERENCE: NOOP's detector also uses the accelerometer/gravity
channel (`motionThreshold = 0.20`) to reject elevated HR that is not movement —
a fever, a stressful call, a hot bath. This module has heart rate only, because
the adapter resolves an HR table but no motion stream (SCHEMA_NOTES.md open
question 3). So this is a **strictly weaker** detector than NOOP's and will
suggest bouts that NOOP would reject. That is the whole reason suggestions
require confirmation rather than being written straight to the log.

Everything here is a pure function over samples.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Sequence

# --- NOOP WorkoutDetector constants, verbatim ------------------------------
MIN_EXERCISE_MIN = 5.0
HR_MARGIN_BPM = 15.0
MERGE_GAP_S = 150.0
RESTING_PERCENTILE = 10.0

# --- local additions, and why they are needed -------------------------------
#
# NOOP's `resting + 15 bpm` works because NOOP *also* requires motion. Without
# that channel the same threshold fires on ordinary waking life: measured against
# a full day of real samples, resting(p10) + 15 sat below a third of the day and
# produced 17 "workouts" in 24 hours — a commute, standing up, and a warm room.
#
# Two extra gates replace the missing motion check:

TYPICAL_PERCENTILE = 50.0
"""Membership threshold is measured against the day's MEDIAN as well as its
floor, so a bout has to stand out from your typical day, not just from sleep."""

INTENSITY_MARGIN_MULT = 2.0
"""The bout's MEAN must clear resting + 2x margin (i.e. +30 bpm). This stands in
for NOOP's `minIntensityZ2Plus = 0.50` zone check, which needs an HRmax we do
not have without a profile.

COST OF THIS: genuinely low-intensity sessions — easy yoga, a flat walk,
mobility work — will NOT be suggested, because with heart rate alone they are
indistinguishable from sitting at a desk. Log those by hand. Loosening
`intensity_mult` toward 1.0 trades that miss for a flood of false positives.
"""

MAX_SAMPLE_GAP_S = 300
"""A hole longer than this breaks a bout: we cannot claim the HR stayed up
across five minutes of missing samples."""

MIN_SAMPLES = 5
"""Below this a 'bout' is noise, however long its timestamps span."""


@dataclass(frozen=True)
class Suggestion:
    """A candidate workout awaiting confirmation."""

    key: str
    day: str
    start_ts: int
    end_ts: int
    duration_s: int
    avg_hr: float
    peak_hr: int
    n_samples: int
    threshold_bpm: float
    resting_bpm: float
    typical_bpm: float
    intensity_floor_bpm: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "day": self.day,
            "start_ts": self.start_ts,
            "end_ts": self.end_ts,
            "duration_s": self.duration_s,
            "duration_min": round(self.duration_s / 60.0, 1),
            "avg_hr": round(self.avg_hr, 1),
            "peak_hr": self.peak_hr,
            "n_samples": self.n_samples,
            "threshold_bpm": round(self.threshold_bpm, 1),
            "resting_bpm": round(self.resting_bpm, 1),
            "typical_bpm": round(self.typical_bpm, 1),
            "intensity_floor_bpm": round(self.intensity_floor_bpm, 1),
            "method": "elevated heart rate only (no motion channel) — approximate",
        }


def percentile(values: Sequence[float], pct: float) -> float:
    """Linear-interpolated percentile. `pct` in [0, 100]."""
    if not values:
        raise ValueError("percentile of an empty sequence")
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    rank = (pct / 100.0) * (len(ordered) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(ordered) - 1)
    frac = rank - lo
    return float(ordered[lo] + (ordered[hi] - ordered[lo]) * frac)


def suggestion_key(start_ts: int) -> str:
    """Stable identity for a suggestion, so a dismissal survives re-detection.

    Keyed on the start minute: re-running detection over the same day yields the
    same key, and a bout whose tail grows as more samples arrive stays the same
    suggestion rather than reappearing as a new one.
    """
    return f"w{int(start_ts) // 60}"


def _runs_above(samples: Sequence[dict[str, Any]], threshold: float) -> list[list[dict[str, Any]]]:
    """Consecutive samples at or above threshold, broken by long data gaps."""
    runs: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    previous_ts: int | None = None

    for sample in samples:
        ts, bpm = int(sample["ts"]), float(sample["bpm"])
        gap_broken = previous_ts is not None and (ts - previous_ts) > MAX_SAMPLE_GAP_S
        if bpm >= threshold and not gap_broken:
            current.append(sample)
        elif bpm >= threshold:
            if current:
                runs.append(current)
            current = [sample]
        else:
            if current:
                runs.append(current)
            current = []
        previous_ts = ts

    if current:
        runs.append(current)
    return runs


def _merge(runs: list[list[dict[str, Any]]], gap_s: float) -> list[list[dict[str, Any]]]:
    """Join runs separated by less than `gap_s` — one bout with a breather in it."""
    if not runs:
        return []
    merged = [list(runs[0])]
    for run in runs[1:]:
        previous_end = int(merged[-1][-1]["ts"])
        if int(run[0]["ts"]) - previous_end < gap_s:
            merged[-1].extend(run)
        else:
            merged.append(list(run))
    return merged


def detect(
    samples: Iterable[dict[str, Any]],
    day: str,
    min_minutes: float = MIN_EXERCISE_MIN,
    hr_margin: float = HR_MARGIN_BPM,
    merge_gap_s: float = MERGE_GAP_S,
    intensity_mult: float = INTENSITY_MARGIN_MULT,
) -> list[Suggestion]:
    """Find elevated-HR blocks in one day's samples.

    A bout must clear three gates:
      1. every sample at or above `max(resting, typical) + hr_margin`
      2. at least `min_minutes` long after merging across short breaks
      3. mean HR at or above `resting + intensity_mult * hr_margin`

    Gates 1 (the median half) and 3 are additions to NOOP's constants, standing
    in for the motion channel we do not have. See the module docstring.

    Returns [] rather than raising when there is too little data to say anything.
    """
    ordered = sorted(
        ({"ts": int(s["ts"]), "bpm": float(s["bpm"])} for s in samples),
        key=lambda s: s["ts"],
    )
    if len(ordered) < MIN_SAMPLES:
        return []

    bpms = [s["bpm"] for s in ordered]
    resting = percentile(bpms, RESTING_PERCENTILE)
    typical = percentile(bpms, TYPICAL_PERCENTILE)
    threshold = max(resting, typical) + hr_margin
    intensity_floor = resting + intensity_mult * hr_margin

    min_seconds = min_minutes * 60.0
    out: list[Suggestion] = []

    for run in _merge(_runs_above(ordered, threshold), merge_gap_s):
        if len(run) < MIN_SAMPLES:
            continue
        start_ts, end_ts = int(run[0]["ts"]), int(run[-1]["ts"])
        duration = end_ts - start_ts
        if duration < min_seconds:
            continue
        run_bpms = [s["bpm"] for s in run]
        mean_hr = sum(run_bpms) / len(run_bpms)
        if mean_hr < intensity_floor:
            continue
        out.append(Suggestion(
            key=suggestion_key(start_ts),
            day=day,
            start_ts=start_ts,
            end_ts=end_ts,
            duration_s=duration,
            avg_hr=mean_hr,
            peak_hr=int(max(run_bpms)),
            n_samples=len(run),
            threshold_bpm=threshold,
            resting_bpm=resting,
            typical_bpm=typical,
            intensity_floor_bpm=intensity_floor,
        ))
    return out


def overlaps(span: tuple[int, int], spans: Iterable[tuple[int, int]]) -> bool:
    """True if `span` intersects any of `spans`.

    Used to hide a suggestion that a logged workout already covers, even when it
    was logged by hand rather than confirmed from this suggestion.
    """
    start, end = span
    return any(start < other_end and other_start < end for other_start, other_end in spans)


def filter_suggestions(
    suggestions: Sequence[Suggestion],
    dismissed: set[str],
    confirmed: set[str],
    logged_spans: Sequence[tuple[int, int]] = (),
) -> list[Suggestion]:
    """Drop suggestions that were dismissed, confirmed, or already covered."""
    out = []
    for suggestion in suggestions:
        if suggestion.key in dismissed or suggestion.key in confirmed:
            continue
        if overlaps((suggestion.start_ts, suggestion.end_ts), logged_spans):
            continue
        out.append(suggestion)
    return out


# --- history summaries ------------------------------------------------------


def weekly_volume(
    workouts: Sequence[dict[str, Any]],
    start_day: str | None = None,
    end_day: str | None = None,
) -> list[dict[str, Any]]:
    """Total minutes and count per ISO week, ascending by week.

    When the window is given, **every** week in it is emitted, including the ones
    with nothing in them. Charting only the weeks that happen to contain a
    workout would draw a sporadic month as an unbroken run of training.
    """
    from datetime import date, timedelta

    def week_key(day: date) -> str:
        iso = day.isocalendar()
        return f"{iso.year}-W{iso.week:02d}"

    buckets: dict[str, dict[str, Any]] = {}

    if start_day and end_day:
        cursor = date.fromisoformat(start_day)
        last = date.fromisoformat(end_day)
        while cursor <= last:
            buckets.setdefault(week_key(cursor),
                               {"week": week_key(cursor), "minutes": 0.0, "count": 0,
                                "start_day": None, "end_day": None})
            cursor += timedelta(days=1)

    for workout in workouts:
        day = workout.get("day")
        if not isinstance(day, str):
            continue
        try:
            parsed = date.fromisoformat(day)
        except ValueError:
            continue
        key = week_key(parsed)
        bucket = buckets.setdefault(key, {"week": key, "minutes": 0.0, "count": 0,
                                          "start_day": None, "end_day": None})
        bucket["minutes"] += (workout.get("duration_s") or 0) / 60.0
        bucket["count"] += 1
        bucket["start_day"] = day if bucket["start_day"] is None else min(bucket["start_day"], day)
        bucket["end_day"] = day if bucket["end_day"] is None else max(bucket["end_day"], day)

    return [
        {**b, "minutes": round(b["minutes"], 1)}
        for b in sorted(buckets.values(), key=lambda b: b["week"])
    ]


def strain_recovery_points(daily_rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Days having BOTH strain and recovery, for the scatter.

    A day missing either is omitted rather than plotted at zero — a point at the
    origin is a claim about a day the strap said nothing about.
    """
    points = []
    for row in daily_rows:
        strain, recovery = row.get("strain"), row.get("recovery")
        if strain is None or recovery is None:
            continue
        points.append({"day": row.get("day"), "strain": float(strain),
                       "recovery": float(recovery)})
    return points
