"""Workout suggestion tests — synthetic HR days with known answers.

The detector has no motion channel, so its whole job is to be *stricter* than a
naive threshold and honest about what it misses. These tests pin both halves:
what it must find, and what it must refuse to suggest.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.workout_detect import (  # noqa: E402
    HR_MARGIN_BPM,
    MIN_EXERCISE_MIN,
    detect,
    filter_suggestions,
    overlaps,
    percentile,
    strain_recovery_points,
    suggestion_key,
    weekly_volume,
)

DAY = "2026-07-01"
MIDNIGHT = 1782518400          # 2026-07-01T00:00:00Z


def day_samples(base_bpm: int = 60, minutes: int = 1440) -> list[dict]:
    """A flat, boring day at `base_bpm`, one sample per minute."""
    return [{"ts": MIDNIGHT + i * 60, "bpm": base_bpm} for i in range(minutes)]


def with_block(samples: list[dict], start_min: int, length_min: int, bpm: int) -> list[dict]:
    out = [dict(s) for s in samples]
    for i in range(start_min, min(start_min + length_min, len(out))):
        out[i]["bpm"] = bpm
    return out


# --- percentile ------------------------------------------------------------


def test_percentile_endpoints_and_interpolation():
    values = [10, 20, 30, 40, 50]
    assert percentile(values, 0) == 10
    assert percentile(values, 100) == 50
    assert percentile(values, 50) == 30
    assert percentile(values, 25) == 20


def test_percentile_of_single_value():
    assert percentile([42], 10) == 42


def test_percentile_of_empty_raises():
    with pytest.raises(ValueError):
        percentile([], 50)


# --- core detection --------------------------------------------------------


def test_finds_a_clear_workout_block():
    samples = with_block(day_samples(60), start_min=600, length_min=45, bpm=150)
    found = detect(samples, DAY)
    assert len(found) == 1
    bout = found[0]
    assert bout.day == DAY
    assert bout.duration_s == 44 * 60          # first to last sample
    assert bout.avg_hr == 150
    assert bout.peak_hr == 150


def test_ignores_a_block_shorter_than_the_minimum():
    samples = with_block(day_samples(60), start_min=600, length_min=3, bpm=150)
    assert detect(samples, DAY) == []


def test_block_exactly_at_the_minimum_is_kept():
    length = int(MIN_EXERCISE_MIN) + 1        # +1 sample so the span reaches 5 min
    samples = with_block(day_samples(60), start_min=600, length_min=length, bpm=150)
    assert len(detect(samples, DAY)) == 1


def test_ordinary_daytime_elevation_is_not_a_workout():
    """The failure this detector exists to avoid.

    A day that is 60 bpm asleep and 78 bpm awake clears NOOP's `resting + 15`
    for two thirds of its length. Without a motion channel that would suggest a
    sixteen-hour workout, so the median and intensity gates must reject it.
    """
    samples = [
        {"ts": MIDNIGHT + i * 60, "bpm": 60 if i < 480 else 78}
        for i in range(1440)
    ]
    assert detect(samples, DAY) == []


def test_real_workout_still_found_inside_a_normal_day():
    samples = [
        {"ts": MIDNIGHT + i * 60, "bpm": 58 if i < 480 else 74}
        for i in range(1440)
    ]
    samples = with_block(samples, start_min=1050, length_min=40, bpm=145)
    found = detect(samples, DAY)
    assert len(found) == 1
    assert found[0].avg_hr == 145


def test_low_intensity_session_is_missed_and_that_is_documented():
    """A gentle session sits below the intensity floor. Documented, not a bug.

    This is the acknowledged cost of having no motion channel: log easy sessions
    by hand. Loosening `intensity_mult` finds it again.
    """
    samples = with_block(day_samples(60), start_min=600, length_min=40, bpm=82)
    assert detect(samples, DAY) == []
    assert len(detect(samples, DAY, intensity_mult=1.0)) == 1


def test_merges_bouts_separated_by_a_short_break():
    samples = day_samples(60)
    samples = with_block(samples, 600, 20, 150)
    samples = with_block(samples, 621, 20, 150)     # 120 s gap, under MERGE_GAP_S
    found = detect(samples, DAY)
    assert len(found) == 1
    # Spans both blocks and the breather between them: minute 600 to minute 640.
    assert found[0].duration_s == 40 * 60
    assert found[0].n_samples == 40          # the gap's samples are not counted


def test_does_not_merge_across_a_long_break():
    samples = day_samples(60)
    samples = with_block(samples, 400, 20, 150)
    samples = with_block(samples, 600, 20, 150)     # hours apart
    assert len(detect(samples, DAY)) == 2


def test_a_long_data_gap_breaks_a_bout():
    """We cannot claim the HR stayed up across missing samples."""
    first = [{"ts": MIDNIGHT + i * 60, "bpm": 150} for i in range(0, 20)]
    second = [{"ts": MIDNIGHT + 20 * 60 + 3600 + i * 60, "bpm": 150} for i in range(0, 20)]
    filler = [{"ts": MIDNIGHT + 90000 + i * 60, "bpm": 55} for i in range(200)]
    found = detect(first + second + filler, DAY)
    assert len(found) == 2


def test_empty_and_tiny_inputs_return_nothing():
    assert detect([], DAY) == []
    assert detect([{"ts": MIDNIGHT, "bpm": 150}], DAY) == []


def test_flat_day_with_no_variation_suggests_nothing():
    assert detect(day_samples(70), DAY) == []


def test_samples_are_sorted_before_detection():
    samples = with_block(day_samples(60), 600, 40, 150)
    shuffled = list(reversed(samples))
    assert detect(shuffled, DAY) == detect(samples, DAY)


def test_reported_thresholds_explain_the_decision():
    samples = with_block(day_samples(60), 600, 40, 150)
    bout = detect(samples, DAY)[0].as_dict()
    assert bout["resting_bpm"] == 60
    assert bout["typical_bpm"] == 60
    assert bout["threshold_bpm"] == 60 + HR_MARGIN_BPM
    assert bout["intensity_floor_bpm"] == 60 + 2 * HR_MARGIN_BPM
    assert "no motion channel" in bout["method"]


# --- suggestion identity ---------------------------------------------------


def test_suggestion_key_is_stable_across_reruns():
    samples = with_block(day_samples(60), 600, 40, 150)
    assert [s.key for s in detect(samples, DAY)] == [s.key for s in detect(samples, DAY)]


def test_suggestion_key_survives_the_bout_growing_a_tail():
    """More samples arriving must not resurrect a dismissed suggestion."""
    short = with_block(day_samples(60), 600, 30, 150)
    longer = with_block(day_samples(60), 600, 45, 150)
    assert detect(short, DAY)[0].key == detect(longer, DAY)[0].key


def test_suggestion_key_buckets_to_the_minute():
    assert suggestion_key(1782518400) == suggestion_key(1782518459)
    assert suggestion_key(1782518400) != suggestion_key(1782518460)


# --- filtering -------------------------------------------------------------


def test_dismissed_and_confirmed_suggestions_are_hidden():
    found = detect(with_block(day_samples(60), 600, 40, 150), DAY)
    key = found[0].key
    assert filter_suggestions(found, {key}, set()) == []
    assert filter_suggestions(found, set(), {key}) == []
    assert filter_suggestions(found, set(), set()) == found


def test_a_manually_logged_workout_hides_an_overlapping_suggestion():
    found = detect(with_block(day_samples(60), 600, 40, 150), DAY)
    span = (found[0].start_ts + 300, found[0].end_ts - 300)
    assert filter_suggestions(found, set(), set(), [span]) == []


def test_a_workout_elsewhere_in_the_day_does_not_hide_it():
    found = detect(with_block(day_samples(60), 600, 40, 150), DAY)
    assert filter_suggestions(found, set(), set(), [(MIDNIGHT, MIDNIGHT + 600)]) == found


def test_overlaps_is_half_open():
    assert overlaps((10, 20), [(15, 30)])
    assert overlaps((10, 20), [(5, 15)])
    assert not overlaps((10, 20), [(20, 30)])      # touching, not overlapping
    assert not overlaps((10, 20), [(0, 10)])
    assert not overlaps((10, 20), [])


# --- summaries -------------------------------------------------------------


def test_weekly_volume_totals_by_iso_week():
    workouts = [
        {"day": "2026-06-29", "duration_s": 1800},   # Monday, W27
        {"day": "2026-07-01", "duration_s": 3600},   # same week
        {"day": "2026-07-06", "duration_s": 1800},   # next week
    ]
    weeks = weekly_volume(workouts)
    assert [w["minutes"] for w in weeks] == [90.0, 30.0]
    assert [w["count"] for w in weeks] == [2, 1]


def test_weekly_volume_emits_empty_weeks_across_the_window():
    """A sporadic month must not draw as an unbroken run of training."""
    weeks = weekly_volume([{"day": "2026-07-01", "duration_s": 3600}],
                          start_day="2026-06-01", end_day="2026-07-31")
    assert len(weeks) >= 8
    assert sum(1 for w in weeks if w["count"] == 0) >= 7
    assert [w["week"] for w in weeks] == sorted(w["week"] for w in weeks)


def test_weekly_volume_ignores_malformed_days():
    assert weekly_volume([{"day": "not-a-date", "duration_s": 60}, {"duration_s": 60}]) == []


def test_weekly_volume_of_nothing_is_empty():
    assert weekly_volume([]) == []


def test_scatter_omits_days_missing_either_axis():
    rows = [
        {"day": "2026-07-01", "strain": 12.0, "recovery": 60.0},
        {"day": "2026-07-02", "strain": None, "recovery": 55.0},
        {"day": "2026-07-03", "strain": 8.0, "recovery": None},
        {"day": "2026-07-04", "strain": 5.0, "recovery": 90.0},
    ]
    points = strain_recovery_points(rows)
    assert [p["day"] for p in points] == ["2026-07-01", "2026-07-04"]


def test_scatter_keeps_genuine_zeros():
    """A real strain of 0 is data; only a missing value is dropped."""
    points = strain_recovery_points([{"day": "2026-07-01", "strain": 0.0, "recovery": 50.0}])
    assert len(points) == 1
