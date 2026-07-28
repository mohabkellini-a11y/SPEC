"""Descriptive statistics over NOOP's daily series.

Scope discipline: nothing here invents a physiological metric. NOOP already
computed recovery/HRV/strain/sleep on-device; this module only summarises those
numbers over time — rolling means, period comparisons, coverage, a least-squares
slope. Every output is descriptive, and a trend line is not a claim about cause.

Missing days are first-class. A gap in the strap record is a gap in the chart,
never an interpolated value, and every summary reports how many days actually
carried data so a 90-day average over 11 nights cannot masquerade as a 90-day
average.

All functions are pure and take `list[float | None]` aligned to a dense list of
day keys.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Iterable, Sequence

Number = float | int | None


def dense_days(start_day: str, end_day: str) -> list[str]:
    """Every calendar day in [start, end] inclusive, ascending.

    The series is densified against this so a missing row becomes an explicit
    None rather than a silently shorter list.
    """
    start, end = date.fromisoformat(start_day), date.fromisoformat(end_day)
    if end < start:
        return []
    return [(start + timedelta(days=i)).isoformat() for i in range((end - start).days + 1)]


def align(rows: Iterable[dict[str, Any]], days: Sequence[str], field_name: str) -> list[float | None]:
    """Project `field_name` from day-keyed rows onto a dense day list."""
    by_day: dict[str, Any] = {}
    for row in rows:
        key = row.get("day")
        if isinstance(key, str):
            by_day[key] = row.get(field_name)
    out: list[float | None] = []
    for day in days:
        value = by_day.get(day)
        out.append(float(value) if isinstance(value, (int, float)) else None)
    return out


def rolling_mean(values: Sequence[Number], window: int, min_periods: int = 1) -> list[float | None]:
    """Trailing rolling mean over the last `window` positions.

    Positions with no value still consume a slot in the window — a 7-day rolling
    mean means seven *days*, not the last seven readings, so a week with two
    nights of data does not silently become a two-night average carried forward.
    `min_periods` is the floor on how many of those days must have data.
    """
    if window <= 0:
        raise ValueError("window must be positive")
    out: list[float | None] = []
    for i in range(len(values)):
        chunk = [v for v in values[max(0, i - window + 1): i + 1] if v is not None]
        out.append(sum(chunk) / len(chunk) if len(chunk) >= min_periods and chunk else None)
    return out


@dataclass(frozen=True)
class Summary:
    """Descriptive summary of one series."""

    n_days: int
    n_values: int
    coverage: float
    mean: float | None = None
    sd: float | None = None
    minimum: float | None = None
    maximum: float | None = None
    latest: float | None = None
    latest_day: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "n_days": self.n_days,
            "n_values": self.n_values,
            "coverage": round(self.coverage, 3),
            "mean": _round(self.mean),
            "sd": _round(self.sd),
            "min": _round(self.minimum),
            "max": _round(self.maximum),
            "latest": _round(self.latest),
            "latest_day": self.latest_day,
        }


def summarize(values: Sequence[Number], days: Sequence[str] | None = None) -> Summary:
    """Mean/SD/min/max/latest plus how much of the window actually had data."""
    present = [(i, v) for i, v in enumerate(values) if v is not None]
    n_days = len(values)
    if not present:
        return Summary(n_days=n_days, n_values=0, coverage=0.0)

    nums = [v for _i, v in present]
    mean = sum(nums) / len(nums)
    # Sample SD (ddof=1), matching how NOOP's analytics report dispersion.
    sd = math.sqrt(sum((x - mean) ** 2 for x in nums) / (len(nums) - 1)) if len(nums) > 1 else None
    last_i, last_v = present[-1]

    return Summary(
        n_days=n_days,
        n_values=len(nums),
        coverage=len(nums) / n_days if n_days else 0.0,
        mean=mean,
        sd=sd,
        minimum=min(nums),
        maximum=max(nums),
        latest=last_v,
        latest_day=days[last_i] if days and last_i < len(days) else None,
    )


@dataclass(frozen=True)
class PeriodDelta:
    """Recent window vs the window immediately before it."""

    recent_mean: float | None
    previous_mean: float | None
    delta: float | None
    percent: float | None
    n_recent: int
    n_previous: int
    comparable: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "recent_mean": _round(self.recent_mean),
            "previous_mean": _round(self.previous_mean),
            "delta": _round(self.delta),
            "percent": _round(self.percent, 1),
            "n_recent": self.n_recent,
            "n_previous": self.n_previous,
            "comparable": self.comparable,
        }


def period_delta(values: Sequence[Number], window: int, min_days: int = 3) -> PeriodDelta:
    """Compare the last `window` days against the `window` days before them.

    `comparable` is False when either half is too sparse to mean anything. The
    caller must not render a delta that says it is not comparable — a "+8%" drawn
    from two nights against one is worse than showing nothing.
    """
    if window <= 0:
        raise ValueError("window must be positive")

    recent = [v for v in values[-window:] if v is not None]
    previous = [v for v in values[-2 * window: -window] if v is not None]

    recent_mean = sum(recent) / len(recent) if recent else None
    previous_mean = sum(previous) / len(previous) if previous else None

    comparable = len(recent) >= min_days and len(previous) >= min_days
    delta = percent = None
    if recent_mean is not None and previous_mean is not None:
        delta = recent_mean - previous_mean
        if previous_mean != 0:
            percent = (delta / abs(previous_mean)) * 100.0

    return PeriodDelta(
        recent_mean=recent_mean, previous_mean=previous_mean,
        delta=delta, percent=percent,
        n_recent=len(recent), n_previous=len(previous), comparable=comparable,
    )


def linear_slope(values: Sequence[Number], min_points: int = 4) -> float | None:
    """Least-squares slope in units per day over the days that have data.

    Descriptive only. A slope is a line fitted to noisy points, not a forecast
    and not a cause. Returns None below `min_points`.
    """
    points = [(float(i), float(v)) for i, v in enumerate(values) if v is not None]
    if len(points) < min_points:
        return None

    n = len(points)
    mean_x = sum(x for x, _ in points) / n
    mean_y = sum(y for _, y in points) / n
    denom = sum((x - mean_x) ** 2 for x, _ in points)
    if denom == 0:
        return None
    return sum((x - mean_x) * (y - mean_y) for x, y in points) / denom


@dataclass
class TrendSeries:
    """One metric over one window, with everything the chart needs."""

    metric: str
    days: list[str]
    values: list[float | None]
    rolling: list[float | None] = field(default_factory=list)
    rolling_window: int = 7
    summary: Summary | None = None
    delta: PeriodDelta | None = None
    slope_per_day: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "metric": self.metric,
            "days": self.days,
            "values": [_round(v) for v in self.values],
            "rolling": [_round(v) for v in self.rolling],
            "rolling_window": self.rolling_window,
            "summary": self.summary.as_dict() if self.summary else None,
            "delta": self.delta.as_dict() if self.delta else None,
            "slope_per_day": _round(self.slope_per_day, 4),
            "has_data": bool(self.summary and self.summary.n_values > 0),
        }


def build_series(
    metric: str,
    days: Sequence[str],
    values: Sequence[Number],
    rolling_window: int = 7,
    delta_window: int | None = None,
) -> TrendSeries:
    """Assemble a metric's series, rolling mean, summary, delta and slope."""
    vals = list(values)
    # A 7-day rolling mean needs at least 3 of those days to be honest about
    # being a weekly average; below that the line would jump around on one night.
    min_periods = max(1, min(3, rolling_window))
    window = delta_window or max(1, len(vals) // 2)

    return TrendSeries(
        metric=metric,
        days=list(days),
        values=vals,
        rolling=rolling_mean(vals, rolling_window, min_periods=min_periods),
        rolling_window=rolling_window,
        summary=summarize(vals, days),
        delta=period_delta(vals, window),
        slope_per_day=linear_slope(vals),
    )


def _round(value: float | None, places: int = 2) -> float | None:
    if value is None:
        return None
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    return round(float(value), places)
