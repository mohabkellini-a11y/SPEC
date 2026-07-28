"""Analytics tests — exact values on hand-checkable inputs.

Gaps are the theme: almost every failure mode here is "a missing day quietly
became a number".
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.analytics import (  # noqa: E402
    align,
    build_series,
    dense_days,
    linear_slope,
    period_delta,
    rolling_mean,
    summarize,
)


# --- dense_days / align ----------------------------------------------------


def test_dense_days_is_inclusive():
    assert dense_days("2026-07-01", "2026-07-04") == [
        "2026-07-01", "2026-07-02", "2026-07-03", "2026-07-04"]


def test_dense_days_single_day():
    assert dense_days("2026-07-01", "2026-07-01") == ["2026-07-01"]


def test_dense_days_reversed_range_is_empty():
    assert dense_days("2026-07-04", "2026-07-01") == []


def test_dense_days_spans_month_and_year_boundaries():
    assert dense_days("2026-12-30", "2027-01-02") == [
        "2026-12-30", "2026-12-31", "2027-01-01", "2027-01-02"]


def test_align_inserts_none_for_missing_days():
    """The core honesty guarantee: a missing row is a hole, not a shifted value."""
    days = dense_days("2026-07-01", "2026-07-05")
    rows = [
        {"day": "2026-07-01", "recovery": 60.0},
        {"day": "2026-07-03", "recovery": 70.0},
        {"day": "2026-07-05", "recovery": 55.0},
    ]
    assert align(rows, days, "recovery") == [60.0, None, 70.0, None, 55.0]


def test_align_treats_null_column_as_missing():
    days = dense_days("2026-07-01", "2026-07-02")
    rows = [{"day": "2026-07-01", "recovery": None}, {"day": "2026-07-02", "recovery": 61}]
    assert align(rows, days, "recovery") == [None, 61.0]


def test_align_ignores_rows_outside_the_window():
    days = dense_days("2026-07-02", "2026-07-03")
    rows = [{"day": "2026-06-01", "strain": 9.0}, {"day": "2026-07-03", "strain": 12.0}]
    assert align(rows, days, "strain") == [None, 12.0]


# --- rolling_mean ----------------------------------------------------------


def test_rolling_mean_is_trailing():
    assert rolling_mean([1, 2, 3, 4], window=2) == [1.0, 1.5, 2.5, 3.5]


def test_rolling_mean_window_counts_days_not_readings():
    """Gaps consume window slots — otherwise a stale value drifts forward."""
    # At index 3 the 3-day window is [None, None, 10] -> mean of one value.
    assert rolling_mean([4, None, None, 10], window=3) == [4.0, 4.0, 4.0, 10.0]


def test_rolling_mean_respects_min_periods():
    out = rolling_mean([5, None, None, None, 9], window=3, min_periods=2)
    assert out == [None, None, None, None, None]


def test_rolling_mean_all_none_is_all_none():
    assert rolling_mean([None, None, None], window=2) == [None, None, None]


def test_rolling_mean_empty():
    assert rolling_mean([], window=7) == []


def test_rolling_mean_rejects_bad_window():
    with pytest.raises(ValueError):
        rolling_mean([1, 2], window=0)


# --- summarize -------------------------------------------------------------


def test_summarize_exact_values():
    days = dense_days("2026-07-01", "2026-07-04")
    s = summarize([2.0, 4.0, None, 6.0], days)
    assert s.n_days == 4
    assert s.n_values == 3
    assert s.coverage == 0.75
    assert s.mean == 4.0
    assert s.minimum == 2.0 and s.maximum == 6.0
    assert s.latest == 6.0
    assert s.latest_day == "2026-07-04"


def test_summarize_sd_is_sample_sd():
    s = summarize([2.0, 4.0, 4.0, 4.0, 5.0, 5.0, 7.0, 9.0])
    assert s.sd == pytest.approx(2.13808993, rel=1e-6)     # ddof=1


def test_summarize_single_value_has_no_sd():
    s = summarize([5.0, None])
    assert s.n_values == 1 and s.sd is None and s.mean == 5.0


def test_summarize_empty_series_reports_zero_coverage():
    s = summarize([None, None, None])
    assert s.n_values == 0 and s.coverage == 0.0
    assert s.mean is None and s.latest is None
    assert s.as_dict()["mean"] is None


def test_summarize_latest_day_tracks_the_last_present_value():
    """Not the last day of the window — the last day that actually had data."""
    days = dense_days("2026-07-01", "2026-07-05")
    s = summarize([1.0, 2.0, None, None, None], days)
    assert s.latest == 2.0 and s.latest_day == "2026-07-02"


# --- period_delta ----------------------------------------------------------


def test_period_delta_compares_adjacent_windows():
    values = [10.0, 10.0, 10.0, 20.0, 20.0, 20.0]
    d = period_delta(values, window=3)
    assert d.previous_mean == 10.0
    assert d.recent_mean == 20.0
    assert d.delta == 10.0
    assert d.percent == 100.0
    assert d.comparable is True


def test_period_delta_percent_uses_absolute_base():
    """Skin-temp deviation is signed; a negative base must not flip the sign."""
    d = period_delta([-2.0, -2.0, -2.0, -1.0, -1.0, -1.0], window=3)
    assert d.delta == 1.0
    assert d.percent == 50.0          # improvement of 1 on a base of magnitude 2


def test_period_delta_not_comparable_when_sparse():
    values = [None, None, 10.0, 20.0, None, None]
    d = period_delta(values, window=3, min_days=3)
    assert d.comparable is False


def test_period_delta_handles_missing_previous_window():
    d = period_delta([5.0, 6.0, 7.0], window=3)
    assert d.previous_mean is None
    assert d.delta is None and d.percent is None
    assert d.comparable is False


def test_period_delta_zero_base_gives_no_percent():
    d = period_delta([0.0, 0.0, 0.0, 1.0, 1.0, 1.0], window=3)
    assert d.delta == 1.0
    assert d.percent is None          # would be division by zero


def test_period_delta_rejects_bad_window():
    with pytest.raises(ValueError):
        period_delta([1.0], window=-1)


# --- linear_slope ----------------------------------------------------------


def test_linear_slope_on_a_perfect_line():
    assert linear_slope([0.0, 2.0, 4.0, 6.0, 8.0]) == pytest.approx(2.0)


def test_linear_slope_is_negative_when_declining():
    assert linear_slope([10.0, 8.0, 6.0, 4.0]) == pytest.approx(-2.0)


def test_linear_slope_uses_day_index_not_value_index():
    """A gap must widen the run, not compress the slope."""
    # Values at day 0 and day 4 only: rise 8 over 4 days = 2.0/day.
    assert linear_slope([0.0, None, None, None, 8.0], min_points=2) == pytest.approx(2.0)


def test_linear_slope_needs_enough_points():
    assert linear_slope([1.0, 2.0]) is None
    assert linear_slope([1.0, 2.0], min_points=2) == pytest.approx(1.0)


def test_linear_slope_flat_series_is_zero():
    assert linear_slope([5.0, 5.0, 5.0, 5.0]) == pytest.approx(0.0)


def test_linear_slope_of_empty_is_none():
    assert linear_slope([]) is None


# --- build_series ----------------------------------------------------------


def test_build_series_assembles_everything():
    days = dense_days("2026-07-01", "2026-07-14")
    values = [50.0 + i for i in range(14)]
    s = build_series("recovery", days, values, rolling_window=7, delta_window=7)

    assert s.metric == "recovery"
    assert len(s.days) == len(s.values) == len(s.rolling) == 14
    assert s.summary.n_values == 14
    assert s.delta.comparable is True
    assert s.delta.delta == pytest.approx(7.0)
    assert s.slope_per_day == pytest.approx(1.0)


def test_build_series_with_no_data_is_serialisable():
    days = dense_days("2026-07-01", "2026-07-07")
    s = build_series("recovery", days, [None] * 7)
    payload = s.as_dict()
    assert payload["has_data"] is False
    assert payload["summary"]["n_values"] == 0
    assert all(v is None for v in payload["values"])
    assert payload["slope_per_day"] is None


def test_build_series_rounds_for_transport():
    days = dense_days("2026-07-01", "2026-07-03")
    s = build_series("avg_hrv", days, [1 / 3, 2 / 3, 1.0])
    assert s.as_dict()["values"] == [0.33, 0.67, 1.0]


def test_build_series_preserves_gaps_through_serialisation():
    days = dense_days("2026-07-01", "2026-07-05")
    s = build_series("strain", days, [8.0, None, 12.0, None, 10.0])
    assert s.as_dict()["values"] == [8.0, None, 12.0, None, 10.0]
    assert s.as_dict()["summary"]["coverage"] == 0.6
