"""Correlation tests.

Two themes: the lag arithmetic must be right (getting it backwards is the easiest
mistake here), and the output must refuse to overstate itself on thin data.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.correlations import (  # noqa: E402
    ABSOLUTE_MIN_DAYS,
    MIN_GROUP_DAYS,
    compare,
    median,
    pair_days,
    pearson,
    shift_day,
    split_groups,
)


def days(start: str, n: int) -> list[str]:
    from datetime import date, timedelta
    d = date.fromisoformat(start)
    return [(d + timedelta(days=i)).isoformat() for i in range(n)]


# --- lag arithmetic ---------------------------------------------------------


def test_shift_day_crosses_month_and_year():
    assert shift_day("2026-07-31", 1) == "2026-08-01"
    assert shift_day("2026-12-31", 1) == "2027-01-01"
    assert shift_day("2026-01-01", -1) == "2025-12-31"


def test_lag_one_pairs_a_habit_with_the_next_days_metric():
    """The whole analysis hinges on this."""
    pairs = pair_days({"2026-07-01": 1.0}, {"2026-07-02": 55.0}, lag=1)
    assert len(pairs) == 1
    assert pairs[0]["habit_day"] == "2026-07-01"
    assert pairs[0]["metric_day"] == "2026-07-02"
    assert pairs[0]["metric_value"] == 55.0


def test_lag_zero_pairs_the_same_day():
    pairs = pair_days({"2026-07-01": 1.0}, {"2026-07-01": 55.0}, lag=0)
    assert pairs[0]["metric_day"] == "2026-07-01"


def test_a_pair_needs_both_sides():
    """A day with a habit but no metric contributes nothing, and vice versa."""
    habit = {"2026-07-01": 1.0, "2026-07-02": 1.0}
    metric = {"2026-07-02": 50.0}          # nothing for 2026-07-03
    assert len(pair_days(habit, metric, lag=1)) == 1


def test_unlogged_habit_days_are_dropped_not_zeroed():
    """'I didn't log' is not 'I didn't do it' — dropping is the honest choice."""
    habit = {"2026-07-01": 1.0, "2026-07-02": None}
    metric = {"2026-07-02": 50.0, "2026-07-03": 60.0}
    pairs = pair_days(habit, metric, lag=1)
    assert [p["habit_day"] for p in pairs] == ["2026-07-01"]


def test_pairs_are_ordered_by_day():
    habit = {d: 1.0 for d in reversed(days("2026-07-01", 5))}
    metric = {d: 50.0 for d in days("2026-07-01", 8)}
    pairs = pair_days(habit, metric, lag=1)
    assert [p["habit_day"] for p in pairs] == sorted(p["habit_day"] for p in pairs)


# --- grouping ---------------------------------------------------------------


def test_median_of_odd_and_even_lengths():
    assert median([3, 1, 2]) == 2
    assert median([4, 1, 2, 3]) == 2.5


def test_median_of_empty_raises():
    with pytest.raises(ValueError):
        median([])


def test_bool_habits_split_yes_and_no():
    pairs = [
        {"habit_value": 1.0, "metric_value": 40.0},
        {"habit_value": 1.0, "metric_value": 44.0},
        {"habit_value": 0.0, "metric_value": 60.0},
    ]
    high, low, method = split_groups(pairs, "bool", "Alcohol")
    assert high.label == "Alcohol: yes" and high.n == 2
    assert low.label == "Alcohol: no" and low.n == 1
    assert method == "yes vs no"


def test_scale_habits_split_at_your_own_median():
    pairs = [{"habit_value": float(v), "metric_value": 50.0} for v in (1, 2, 4, 5)]
    high, low, method = split_groups(pairs, "scale", "Screen time")
    assert high.n == 2 and low.n == 2
    assert "median split" in method


def test_group_statistics():
    pairs = [
        {"habit_value": 1.0, "metric_value": 40.0},
        {"habit_value": 1.0, "metric_value": 50.0},
        {"habit_value": 0.0, "metric_value": 60.0},
    ]
    high, _low, _ = split_groups(pairs, "bool", "X")
    assert high.mean == 45.0
    assert high.sd == pytest.approx(7.0710678, rel=1e-6)
    assert high.as_dict()["min"] == 40.0 and high.as_dict()["max"] == 50.0


def test_single_value_group_has_no_sd():
    pairs = [{"habit_value": 1.0, "metric_value": 40.0}]
    high, _low, _ = split_groups(pairs, "bool", "X")
    assert high.sd is None


# --- pearson ----------------------------------------------------------------


def test_pearson_of_a_perfect_positive_relationship():
    pairs = [{"habit_value": float(i), "metric_value": float(i) * 2} for i in range(6)]
    assert pearson(pairs) == pytest.approx(1.0)


def test_pearson_of_a_perfect_negative_relationship():
    pairs = [{"habit_value": float(i), "metric_value": -float(i)} for i in range(6)]
    assert pearson(pairs) == pytest.approx(-1.0)


def test_pearson_refuses_on_too_few_points():
    assert pearson([{"habit_value": 1.0, "metric_value": 2.0}] * 3) is None


def test_pearson_refuses_without_spread():
    pairs = [{"habit_value": 3.0, "metric_value": float(i)} for i in range(6)]
    assert pearson(pairs) is None


# --- compare() --------------------------------------------------------------


def build(habit_values: list[float | None], metric_values: list[float | None],
          start: str = "2026-07-01"):
    day_keys = days(start, max(len(habit_values), len(metric_values)) + 2)
    habit = {day_keys[i]: v for i, v in enumerate(habit_values)}
    metric = {day_keys[i]: v for i, v in enumerate(metric_values)}
    return habit, metric


def test_compare_finds_a_clear_difference():
    # Alcohol on alternating days; HRV the next day is 20 lower after drinking.
    habit_values, metric_values = [], []
    for i in range(20):
        drank = i % 2 == 0
        habit_values.append(1.0 if drank else 0.0)
        metric_values.append(None)
    metric_values = [None] + [40.0 if i % 2 == 0 else 60.0 for i in range(20)]

    habit, metric = build(habit_values, metric_values)
    result = compare(habit, metric, habit_type="bool", habit_label="Alcohol",
                     metric_label="HRV", metric_unit="ms", lag=1)

    assert result["n_pairs"] == 20
    assert result["reliable"] is True
    assert result["difference"] == pytest.approx(-20.0)
    assert result["direction"] == "lower"
    assert "HRV averaged 20 ms lower the next day" in result["summary"]
    # Both group labels appear, so a median split does not read as "with/without".
    assert "Alcohol: yes" in result["summary"]
    assert "Alcohol: no" in result["summary"]


def test_compare_refuses_to_headline_thin_data():
    habit, metric = build([1.0, 0.0, 1.0], [None, 50.0, 60.0, 55.0])
    result = compare(habit, metric, habit_type="bool", habit_label="Alcohol",
                     metric_label="HRV", lag=1)
    assert result["enough_data"] is False
    assert result["reliable"] is False
    assert "Not enough logged days" in result["summary"]
    assert any("Not enough data" in c for c in result["caveats"])


def test_compare_warns_between_the_two_thresholds():
    n = ABSOLUTE_MIN_DAYS + 1
    assert n < MIN_GROUP_DAYS
    habit_values = [1.0] * n + [0.0] * n
    metric_values = [None] + [40.0] * n + [60.0] * n
    habit, metric = build(habit_values, metric_values)
    result = compare(habit, metric, habit_type="bool", habit_label="Alcohol",
                     metric_label="HRV", lag=1)
    assert result["enough_data"] is True
    assert result["reliable"] is False
    assert any("could easily be chance" in c for c in result["caveats"])


def test_scale_summary_names_both_sides_of_the_split():
    """A median split has no 'without' side — both labels must appear."""
    habit_values = [float(v) for v in ([5] * 8 + [1] * 8)]
    metric_values = [None] + [70.0] * 8 + [50.0] * 8
    habit, metric = build(habit_values, metric_values)
    result = compare(habit, metric, habit_type="scale", habit_label="Screen time",
                     metric_label="Recovery", metric_unit="%", lag=1)
    assert "Screen time \u2265" in result["summary"]
    assert "Screen time <" in result["summary"]
    assert "without" not in result["summary"]


def test_every_result_carries_the_not_causal_caveat():
    habit, metric = build([1.0] * 10 + [0.0] * 10,
                          [None] + [40.0] * 10 + [60.0] * 10)
    result = compare(habit, metric, habit_type="bool", habit_label="Alcohol",
                     metric_label="HRV", lag=1)
    assert any("not what caused what" in c for c in result["caveats"])
    assert "Descriptive only" in result["summary"]


def test_lag_one_caveat_explains_the_day_bucketing():
    habit, metric = build([1.0] * 10 + [0.0] * 10,
                          [None] + [40.0] * 10 + [60.0] * 10)
    result = compare(habit, metric, habit_type="bool", habit_label="A",
                     metric_label="HRV", lag=1)
    assert any("the day it ends" in c for c in result["caveats"])


def test_lag_zero_warns_it_is_probably_backwards():
    habit, metric = build([1.0] * 10, [50.0] * 10)
    result = compare(habit, metric, habit_type="bool", habit_label="A",
                     metric_label="HRV", lag=0)
    assert any("Usually you want lag 1" in c for c in result["caveats"])


def test_numeric_habit_reports_pearson_and_bool_does_not():
    habit_values = [float(i % 5 + 1) for i in range(20)]
    metric_values = [None] + [float(50 + (i % 5) * 3) for i in range(20)]
    habit, metric = build(habit_values, metric_values)

    scaled = compare(habit, metric, habit_type="scale", habit_label="Screen time",
                     metric_label="HRV", lag=1)
    assert scaled["pearson_r"] is not None

    boolean = compare(habit, metric, habit_type="bool", habit_label="X",
                      metric_label="HRV", lag=1)
    assert boolean["pearson_r"] is None


def test_compare_with_no_overlap_is_empty_not_an_error():
    result = compare({"2026-07-01": 1.0}, {"2026-09-01": 50.0},
                     habit_type="bool", habit_label="A", metric_label="HRV", lag=1)
    assert result["n_pairs"] == 0
    assert result["difference"] is None
    assert result["direction"] is None
    assert result["enough_data"] is False


def test_pairs_are_returned_for_the_overlay_chart():
    habit, metric = build([1.0, 0.0], [None, 50.0, 60.0])
    result = compare(habit, metric, habit_type="bool", habit_label="A",
                     metric_label="HRV", lag=1)
    assert result["pairs"][0]["habit_day"] < result["pairs"][0]["metric_day"]
