"""Habit-vs-metric comparison. Descriptive only.

What this does: takes a habit you logged on day D and a metric NOOP computed for
day D+lag, splits the days into two groups, and reports the means side by side.

What this is NOT: evidence that the habit caused the difference. You are not
running a controlled trial on yourself. Alcohol on a Friday travels with a late
night, a big meal, a different room and a different week — any of which could
move HRV. Every output carries that caveat, and `reliable` is False whenever the
groups are too small to mean much.

Why lag defaults to 1
---------------------
NOOP attributes a sleep session to the day its **end** falls on
(SCHEMA_NOTES.md §1.4). A drink on Friday evening affects the night that ends
Saturday morning, which NOOP files under Saturday. So "Friday's habit" pairs
with "Saturday's recovery" — lag 1. Using lag 0 would compare Friday's drink
with the recovery from Thursday night, which happened before the drink.

That is the single easiest way to get this analysis backwards, so lag 1 is the
default and the lag is always reported.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Sequence

#: Below this many days in either group, differences are noise. The UI refuses
#: to headline a comparison under this and says why.
MIN_GROUP_DAYS = 5

#: Below this the comparison is not shown at all — there is nothing to compare.
ABSOLUTE_MIN_DAYS = 3


@dataclass
class Group:
    label: str
    values: list[float] = field(default_factory=list)

    @property
    def n(self) -> int:
        return len(self.values)

    @property
    def mean(self) -> float | None:
        return sum(self.values) / len(self.values) if self.values else None

    @property
    def sd(self) -> float | None:
        if len(self.values) < 2:
            return None
        mean = self.mean or 0.0
        return math.sqrt(sum((v - mean) ** 2 for v in self.values) / (len(self.values) - 1))

    def as_dict(self) -> dict[str, Any]:
        return {"label": self.label, "n": self.n,
                "mean": _round(self.mean), "sd": _round(self.sd),
                "min": _round(min(self.values)) if self.values else None,
                "max": _round(max(self.values)) if self.values else None}


def _round(value: float | None, places: int = 2) -> float | None:
    if value is None or (isinstance(value, float) and (math.isnan(value) or math.isinf(value))):
        return None
    return round(float(value), places)


def shift_day(day: str, lag: int) -> str:
    from datetime import date, timedelta
    return (date.fromisoformat(day) + timedelta(days=lag)).isoformat()


def pair_days(
    habit_by_day: dict[str, float],
    metric_by_day: dict[str, float | None],
    lag: int = 1,
) -> list[dict[str, Any]]:
    """Pair a habit on day D with the metric on day D+lag.

    Only days where BOTH sides have a value survive. A habit you did not log is
    not a zero — it is absent, and absent days are dropped rather than assumed.
    """
    pairs = []
    for day, habit_value in sorted(habit_by_day.items()):
        if habit_value is None:
            continue
        target = shift_day(day, lag)
        metric_value = metric_by_day.get(target)
        if metric_value is None:
            continue
        pairs.append({
            "habit_day": day,
            "metric_day": target,
            "habit_value": float(habit_value),
            "metric_value": float(metric_value),
        })
    return pairs


def median(values: Sequence[float]) -> float:
    ordered = sorted(values)
    n = len(ordered)
    if n == 0:
        raise ValueError("median of empty sequence")
    mid = n // 2
    return ordered[mid] if n % 2 else (ordered[mid - 1] + ordered[mid]) / 2.0


def split_groups(pairs: Sequence[dict[str, Any]], habit_type: str,
                 habit_label: str) -> tuple[Group, Group, str]:
    """Divide the paired days into two comparable groups.

    Boolean habits split on yes/no. Scales and numbers split at the median of the
    days you actually logged — not at a fixed threshold, because "a lot of screen
    time" means something different for everyone.
    """
    if habit_type == "bool":
        high = Group(f"{habit_label}: yes")
        low = Group(f"{habit_label}: no")
        for pair in pairs:
            (high if pair["habit_value"] >= 0.5 else low).values.append(pair["metric_value"])
        return high, low, "yes vs no"

    values = [p["habit_value"] for p in pairs]
    if not values:
        return Group(f"{habit_label}: higher"), Group(f"{habit_label}: lower"), "median split"

    cut = median(values)
    high = Group(f"{habit_label} ≥ {_fmt(cut)}")
    low = Group(f"{habit_label} < {_fmt(cut)}")
    for pair in pairs:
        (high if pair["habit_value"] >= cut else low).values.append(pair["metric_value"])
    return high, low, f"median split at {_fmt(cut)}"


def _fmt(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:.1f}"


def pearson(pairs: Sequence[dict[str, Any]]) -> float | None:
    """Pearson r between habit value and metric value. Descriptive.

    Returns None below 4 points or when either side has no spread — a
    correlation over three days is a drawing, not a finding.
    """
    if len(pairs) < 4:
        return None
    xs = [p["habit_value"] for p in pairs]
    ys = [p["metric_value"] for p in pairs]
    mean_x, mean_y = sum(xs) / len(xs), sum(ys) / len(ys)
    sx = math.sqrt(sum((x - mean_x) ** 2 for x in xs))
    sy = math.sqrt(sum((y - mean_y) ** 2 for y in ys))
    if sx == 0 or sy == 0:
        return None
    cov = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    return cov / (sx * sy)


def compare(
    habit_by_day: dict[str, float],
    metric_by_day: dict[str, float | None],
    *,
    habit_type: str,
    habit_label: str,
    metric_label: str,
    metric_unit: str = "",
    lag: int = 1,
) -> dict[str, Any]:
    """Full comparison payload, including every caveat that applies."""
    pairs = pair_days(habit_by_day, metric_by_day, lag)
    high, low, split_method = split_groups(pairs, habit_type, habit_label)

    difference = None
    if high.mean is not None and low.mean is not None:
        difference = high.mean - low.mean

    smallest = min(high.n, low.n)
    enough = smallest >= ABSOLUTE_MIN_DAYS
    reliable = smallest >= MIN_GROUP_DAYS

    caveats: list[str] = [
        "Descriptive only — this shows what happened alongside what, not what "
        "caused what.",
    ]
    if not enough:
        caveats.append(
            f"Not enough data: the smaller group has {smallest} day(s). "
            f"Log more days before reading anything into this."
        )
    elif not reliable:
        caveats.append(
            f"The smaller group has only {smallest} days (under {MIN_GROUP_DAYS}), "
            "so this difference could easily be chance."
        )
    if lag == 1:
        caveats.append(
            "Lag 1: a habit on day D is compared with day D+1's metric, because "
            "NOOP files a night's sleep under the day it ends."
        )
    elif lag == 0:
        caveats.append(
            "Lag 0 compares a habit with the SAME day's metric — for sleep-derived "
            "metrics that is the night BEFORE the habit. Usually you want lag 1."
        )
    caveats.append(
        "Habits and metrics move together for many reasons. A late night, a big "
        "meal and a drink tend to happen on the same evening."
    )

    return {
        "lag": lag,
        "habit_label": habit_label,
        "habit_type": habit_type,
        "metric_label": metric_label,
        "metric_unit": metric_unit,
        "n_pairs": len(pairs),
        "split_method": split_method,
        "groups": [high.as_dict(), low.as_dict()],
        "difference": _round(difference),
        "direction": (
            None if difference is None or abs(difference) < 1e-9
            else "higher" if difference > 0 else "lower"
        ),
        "pearson_r": _round(pearson(pairs), 3) if habit_type != "bool" else None,
        "enough_data": enough,
        "reliable": reliable,
        "min_group_days": MIN_GROUP_DAYS,
        "summary": _summary(high, low, difference, metric_label, metric_unit, lag, enough),
        "caveats": caveats,
        "pairs": pairs,
    }


def _summary(high: Group, low: Group, difference: float | None,
             metric_label: str, metric_unit: str, lag: int, enough: bool) -> str:
    if not enough or difference is None:
        return "Not enough logged days to compare yet."
    unit = f" {metric_unit}" if metric_unit else ""
    direction = "higher" if difference > 0 else "lower"
    when = "the next day" if lag == 1 else f"{lag} day(s) later" if lag else "the same day"
    return (
        f"{metric_label} averaged {_fmt(abs(difference))}{unit} {direction} {when} "
        f"after {high.n} days of \u201c{high.label}\u201d "
        f"(mean {_fmt(high.mean or 0)}{unit}) than after {low.n} days of "
        f"\u201c{low.label}\u201d (mean {_fmt(low.mean or 0)}{unit}). "
        "Descriptive only."
    )
