"""Metric metadata — label, unit, and provenance for every value we display.

The requirement "every derived metric labeled as an approximation, not clinical
data" is enforced structurally here rather than by a footnote: the UI renders
tiles from this table, and a tile cannot exist without declaring what it is.

`kind`:
  measured    — read off the strap's sensors (still not clinical-grade)
  derived     — computed by NOOP on-device from its own documented method
  approximate — derived AND explicitly labelled APPROXIMATE in NOOP's source

`method` cites where NOOP's number comes from, at the pinned commit.
"""

from __future__ import annotations

from typing import Any

METRIC_META: dict[str, dict[str, Any]] = {
    "recovery": {
        "label": "Recovery", "unit": "%", "kind": "approximate", "precision": 0,
        "method": "Weighted robust-z composite (HRV .60 / RHR .20 / resp .05 / sleep .15) "
                  "through a logistic anchored at 58%. NOOP RecoveryScorer.",
        "note": "Not WHOOP's score. A transparent, HRV-dominant proxy.",
        "bands": [{"max": 34, "name": "low", "color": "red"},
                  {"max": 67, "name": "moderate", "color": "yellow"},
                  {"max": 101, "name": "high", "color": "green"}],
    },
    "avg_hrv": {
        "label": "HRV", "unit": "ms", "kind": "derived", "precision": 0,
        "method": "RMSSD (Task Force 1996) over 5-min windows, Malik 20% ectopic filter. "
                  "NOOP HRVAnalyzer.",
        "note": "RMSSD, not SDNN. Nightly average.",
    },
    "resting_hr": {
        "label": "Resting HR", "unit": "bpm", "kind": "derived", "precision": 0,
        "method": "Minimum of 5-minute rolling-mean HR across the in-bed window. "
                  "NOOP RecoveryScorer.restingHR.",
    },
    "total_sleep_min": {
        "label": "Sleep", "unit": "min", "kind": "approximate", "precision": 0,
        "method": "AASM total sleep time from gravity-stillness detection, HR-confirmed. "
                  "NOOP SleepStager.",
        "note": "Sleep detection and staging are labelled APPROXIMATE in NOOP's own source.",
    },
    "efficiency": {
        "label": "Sleep efficiency", "unit": "%", "kind": "approximate", "precision": 0,
        "scale": 100.0,
        "method": "TST / time-in-bed. NOOP SleepStager.",
    },
    "strain": {
        "label": "Day strain", "unit": "/21", "kind": "approximate", "precision": 1,
        "method": "Edwards/Banister TRIMP mapped to a 0-21 logarithmic scale, "
                  "Tanaka HRmax. NOOP StrainScorer.",
        "note": "Not WHOOP's strain. Same family of method, different constants.",
    },
    "skin_temp_dev_c": {
        "label": "Skin temp", "unit": "°C dev", "kind": "approximate", "precision": 2,
        "method": "Wear-gated mean in-bed skin temperature minus personal baseline. "
                  "NOOP AnalyticsEngine.",
        "note": "A deviation from your baseline, not an absolute temperature, and "
                "not a body/core temperature.",
        "signed": True,
    },
    "resp_rate_bpm": {
        "label": "Respiratory rate", "unit": "/min", "kind": "derived", "precision": 1,
        "method": "Peak detection on the 1 Hz respiration channel. NOOP SleepStager.",
    },
    "spo2_pct": {
        "label": "SpO₂", "unit": "%", "kind": "measured", "precision": 0,
        "method": "Strap sensor, measured during sleep only.",
        "note": "NOOP's analytics never populate this; it comes from strap decode or "
                "an imported history file. An empty tile may be genuine.",
    },
    "deep_min": {"label": "Deep", "unit": "min", "kind": "approximate", "precision": 0,
                 "method": "4-class staging. NOOP SleepStager."},
    "rem_min": {"label": "REM", "unit": "min", "kind": "approximate", "precision": 0,
                "method": "4-class staging. NOOP SleepStager."},
    "light_min": {"label": "Light", "unit": "min", "kind": "approximate", "precision": 0,
                  "method": "4-class staging. NOOP SleepStager."},
    "disturbances": {"label": "Disturbances", "unit": "", "kind": "approximate", "precision": 0,
                     "method": "Wake segments within a detected sleep session."},
    "steps": {"label": "Steps", "unit": "", "kind": "measured", "precision": 0,
              "method": "Strap accelerometer step counter."},
    "active_kcal_est": {"label": "Active calories", "unit": "kcal", "kind": "approximate",
                        "precision": 0, "method": "HR-based estimate. NOOP Calories.",
                        "note": "An estimate, and a loose one."},
    "exercise_count": {"label": "Workouts", "unit": "", "kind": "derived", "precision": 0,
                       "method": "Elevated-HR block detection. NOOP WorkoutDetector."},
    "heart_rate": {"label": "Heart rate", "unit": "bpm", "kind": "measured", "precision": 0,
                   "method": "Strap PPG, ~1 Hz."},
    "battery": {"label": "Strap battery", "unit": "%", "kind": "measured", "precision": 0,
                "method": "Strap-reported battery level."},
}

TODAY_TILES: tuple[str, ...] = (
    "recovery", "avg_hrv", "resting_hr", "total_sleep_min", "strain",
    "heart_rate", "skin_temp_dev_c", "battery",
)
"""Tile order on the Today screen, per the Phase 1 spec."""

TREND_METRICS: tuple[str, ...] = (
    "recovery", "avg_hrv", "resting_hr", "total_sleep_min", "strain",
)
"""Chart order on the Trends screen, per the Phase 2 spec.

Only fields that live on NOOP's daily record can be charted — `heart_rate` and
`battery` are live readings with no per-day history, so they are deliberately
absent rather than plotted as a flat line.
"""

LOWER_IS_BETTER: frozenset[str] = frozenset({"resting_hr"})
"""Metrics where a downward trend is the favourable direction.

Used only to colour a delta, never to make a recommendation. Sleep and strain
are intentionally not in here: more sleep is not always better and strain has no
'good' direction at all.
"""

NEUTRAL_DIRECTION: frozenset[str] = frozenset({"strain", "skin_temp_dev_c"})
"""Metrics with no favourable direction — deltas render without a colour."""

DISCLAIMER = (
    "Not a medical device. Every value here is an approximation computed on your "
    "own machine from published methods — not clinically validated, not medical "
    "advice, and not WHOOP's proprietary scores."
)
