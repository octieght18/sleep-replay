"""Resampling primitives for the Aligner (processing/aligner.py composes them).

Requirements 9.8, 10.4-10.12, 10.14, 10.15, 10.17. Pure functions over
per-metric series that the Aligner has already converted to the metric's
Canonical_Unit and to UTC, sorted, deduplicated (10.1-10.3) and reduced to one
source (10.13). A *series* is a sequence of ``(timestamp, value)`` pairs with
timezone-aware timestamps; the functions sort defensively (so input order
never matters, 10.18) but reject duplicate timestamps, since collapsing them
is the Aligner's job (10.2).

Conventions (documented choices):

* **Grid.** Sample ``k`` has timestamp ``start + k * resolution`` in UTC
  elapsed time, for every ``k >= 0`` with that instant earlier than the
  session ``end_time`` (10.6, 9.8); :func:`sample_grid` returns it. The count
  is ``ceil(duration / resolution)`` (10.15).
* **Sample_Interval.** Sample ``i`` owns ``[grid[i], grid[i + 1])``; the last
  owns ``[grid[-1], session_end)``. Intervals are half-open, so a measured
  value timestamped exactly at the session ``end_time`` lies in *no*
  Sample_Interval: it never contributes to a per-interval mean (10.7). It is
  still a measured value within the SleepSession (session telemetry is the
  closed range ``[start_time, end_time]``), so it counts for Timeline_Resolution
  selection (10.4) and serves as the "nearest measured value after" bracket
  when the last Sample_Interval is empty (10.8).
* **In-session values only.** Measured values outside ``[start, end]`` are
  ignored, so every non-missing sample lies within the range of the measured
  values used within the SleepSession (10.14).
* **Interpolation brackets (10.8, 10.9).** When sample ``i``'s interval holds
  no measured value, the brackets are the nearest measured value *before the
  Sample_Interval* (the latest with timestamp ``< grid[i]``) and the nearest
  *after* it (the earliest with timestamp ``>= grid[i + 1]``, or
  ``>= session_end`` for the last sample). Because the interval is empty,
  "strictly before the interval" and "at or before the sample timestamp"
  select the same value. The separation ``after - before`` is compared to
  Max_Interpolation_Gap with exact ``timedelta`` arithmetic: ``<=`` interpolates
  at the sample timestamp, ``>`` marks the sample missing and yields one
  :class:`Gap` per bracketing pair (not per sample). A missing bracket on
  either side means the interval lies entirely before the first or after the
  last measured value: missing, no gap entry, no extrapolation (10.10).
* **Float guard.** Means and interpolated values are clamped to the
  ``[min, max]`` of the measured values they were computed from.
* **Steps (10.12, 10.17).** A steps value applies unchanged to every sample
  timestamp in ``[ts, ts + 60 s)``. If two steps minutes overlap (only
  possible with non-minute-aligned input), the sample takes the value of the
  latest one starting at or before it. Samples in no steps minute are missing.
* **Stages (10.11).** A sample gets the stage of the Stage_Segment whose
  ``[start_time, end_time)`` contains its timestamp, else ``unknown``.

* **Timestamps.** Every timestamp must be timezone-aware (``ValueError``
  otherwise); all comparisons and arithmetic use UTC instants, so results do
  not depend on the Display_Timezone or on the offsets the inputs carry (9.5).

Imports only ``backend.domain`` and the standard library (Dependency_Rules,
Requirement 17.2).
"""

from __future__ import annotations

import math
import statistics
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable, Mapping, Sequence

from backend.domain.stages import Sleep_Stage, Stage_Segment
from backend.domain.telemetry import CONTINUOUS_METRICS, Metric
from backend.domain.timeline import (
    FALLBACK_TIMELINE_RESOLUTION_S,
    MAX_INTERPOLATION_GAP,
    STEPS_VALUE_SPAN,
    TIMELINE_RESOLUTION_CANDIDATES_S,
    sample_timestamps,
)

__all__ = [
    "Series",
    "Gap",
    "median_interval",
    "choose_resolution",
    "sample_grid",
    "resample_continuous",
    "resample_steps",
    "assign_stages",
]

#: A per-metric series of ``(timestamp, canonical value)`` pairs.
Series = Sequence[tuple[datetime, float]]


@dataclass(frozen=True)
class Gap:
    """One interpolation gap longer than Max_Interpolation_Gap (Requirement 10.9).

    ``last_value_time`` is the UTC timestamp of the last measured value before
    the gap; ``elapsed`` is the time between the two bracketing measured values.
    """

    metric: Metric
    last_value_time: datetime
    elapsed: timedelta

    def to_report_entry(self) -> dict[str, object]:
        """Processing_Report ``gaps`` entry (metric, last-value timestamp, elapsed)."""
        return {
            "metric": self.metric.value,
            "last_value_time": self.last_value_time.isoformat(),
            "elapsed_seconds": self.elapsed.total_seconds(),
        }


# --------------------------------------------------------------------------- helpers


def _to_utc(dt: datetime) -> datetime:
    """``dt`` as a UTC instant; rejects naive datetimes."""
    if not isinstance(dt, datetime) or dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError(f"timestamps must be timezone-aware datetimes, got {dt!r}")
    return dt.astimezone(timezone.utc)


def _prepare(series: Iterable[tuple[datetime, float]]) -> tuple[list[datetime], list[float]]:
    """UTC-normalize and sort a series; reject duplicates and non-finite values."""
    pairs: list[tuple[datetime, float]] = []
    for ts, value in series:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError(f"series values must be numbers, got {value!r}")
        if not math.isfinite(value):
            raise ValueError(f"series values must be finite, got {value!r}")
        pairs.append((_to_utc(ts), float(value)))
    pairs.sort(key=lambda p: p[0])
    times = [p[0] for p in pairs]
    for a, b in zip(times, times[1:]):
        if a == b:
            raise ValueError(f"duplicate timestamp {a.isoformat()}; collapse duplicates first")
    return times, [p[1] for p in pairs]


def _check_grid(grid: Sequence[datetime], session_end: datetime | None) -> list[datetime]:
    utc = [_to_utc(t) for t in grid]
    for a, b in zip(utc, utc[1:]):
        if b <= a:
            raise ValueError("grid timestamps must be strictly increasing")
    if session_end is not None and utc and _to_utc(session_end) <= utc[-1]:
        raise ValueError("session_end must be later than the last grid timestamp")
    return utc


def _clamp(value: float, lo: float, hi: float) -> float:
    return min(max(value, lo), hi)


# ------------------------------------------------------------- Timeline_Resolution


def median_interval(series: Series) -> timedelta | None:
    """Median interval between consecutive points, or ``None`` for < 2 points.

    For an even number of intervals the median is the mean of the middle two
    (rounded to the nearest microsecond, which cannot change the comparison
    against whole-second candidates in :func:`choose_resolution`).
    """
    times, _ = _prepare(series)
    if len(times) < 2:
        return None
    intervals_us = [(b - a) // timedelta(microseconds=1) for a, b in zip(times, times[1:])]
    return timedelta(microseconds=statistics.median(intervals_us))


def choose_resolution(series_by_metric: Mapping[Metric, Series]) -> int:
    """Timeline_Resolution in seconds (Requirements 10.4, 10.5).

    The smallest candidate ``>=`` the shortest median interval among the
    Continuous_Metrics with at least two points; 60 s when no Continuous_Metric
    has two points or the shortest median exceeds 60 s. Non-continuous metrics
    (steps) are ignored. The caller passes the in-session, deduplicated,
    sensor-selected series.
    """
    medians = [
        m
        for metric, series in series_by_metric.items()
        if metric in CONTINUOUS_METRICS
        for m in (median_interval(series),)
        if m is not None
    ]
    if not medians:
        return FALLBACK_TIMELINE_RESOLUTION_S
    shortest = min(medians)
    for candidate in TIMELINE_RESOLUTION_CANDIDATES_S:
        if timedelta(seconds=candidate) >= shortest:
            return candidate
    return FALLBACK_TIMELINE_RESOLUTION_S


def sample_grid(start_time: datetime, end_time: datetime, resolution_s: int) -> tuple[datetime, ...]:
    """UTC sample timestamps ``start + k * resolution < end`` (Requirements 9.8, 10.6, 10.15)."""
    return sample_timestamps(start_time, end_time, resolution_s)


# ------------------------------------------------------------- Continuous_Metrics


def resample_continuous(
    metric: Metric,
    series: Series,
    grid: Sequence[datetime],
    session_end: datetime,
    max_gap: timedelta | None = None,
) -> tuple[tuple[float | None, ...], tuple[bool, ...], tuple[Gap, ...]]:
    """Resample one Continuous_Metric onto ``grid`` (Requirements 10.7-10.10).

    ``grid`` is the output of :func:`sample_grid` (``grid[0]`` is the session
    start); ``max_gap`` defaults to the metric's Max_Interpolation_Gap.

    Returns ``(values, missing, gaps)``: ``values[i]`` is ``None`` exactly when
    ``missing[i]`` is true; ``gaps`` holds one :class:`Gap` per bracketing pair
    wider than ``max_gap`` that left at least one sample missing, ordered by
    ``last_value_time``. Only measured values within ``[grid[0], session_end]``
    are used. With no such value every sample is missing and ``gaps`` is empty
    (the Aligner reports the metric as unavailable, 10.10).
    """
    if metric not in CONTINUOUS_METRICS:
        raise ValueError(f"{metric!r} is not a Continuous_Metric")
    if max_gap is None:
        max_gap = MAX_INTERPOLATION_GAP[metric]
    if max_gap < timedelta(0):
        raise ValueError("max_gap must not be negative")
    samples = _check_grid(grid, session_end)
    n = len(samples)
    if n == 0:
        return (), (), ()
    end = _to_utc(session_end)
    times, vals = _prepare(series)
    first = bisect_left(times, samples[0])
    last = bisect_right(times, end)
    times, vals = times[first:last], vals[first:last]

    values: list[float | None] = [None] * n
    missing: list[bool] = [True] * n
    gaps: dict[int, Gap] = {}
    for i, t in enumerate(samples):
        interval_end = samples[i + 1] if i + 1 < n else end
        lo = bisect_left(times, t)
        hi = bisect_left(times, interval_end)
        if hi > lo:  # Mean of the values inside [t, interval_end) (10.7).
            inside = vals[lo:hi]
            mean = math.fsum(inside) / len(inside)
            values[i] = _clamp(mean, min(inside), max(inside))
            missing[i] = False
            continue
        before, after = lo - 1, hi
        if before < 0 or after >= len(times):
            continue  # Entirely before the first / after the last value (10.10).
        t0, t1 = times[before], times[after]
        separation = t1 - t0
        if separation <= max_gap:  # Interpolate at the sample timestamp (10.8).
            v0, v1 = vals[before], vals[after]
            fraction = (t - t0) / separation
            values[i] = _clamp(v0 + (v1 - v0) * fraction, min(v0, v1), max(v0, v1))
            missing[i] = False
        elif before not in gaps:  # One entry per gap, not per sample (10.9).
            gaps[before] = Gap(metric=metric, last_value_time=t0, elapsed=separation)
    return tuple(values), tuple(missing), tuple(gaps[k] for k in sorted(gaps))


# ----------------------------------------------------------------------------- Steps


def resample_steps(
    series: Series, grid: Sequence[datetime]
) -> tuple[tuple[float | None, ...], tuple[bool, ...]]:
    """Hold each steps value unchanged over ``[ts, ts + 60 s)`` (Requirements 10.12, 10.17).

    Returns ``(values, missing)``. Steps points before ``grid[0]`` (the session
    start) are ignored, so only in-session values are used (10.14). A sample
    covered by overlapping steps minutes takes the latest-starting one.
    """
    samples = _check_grid(grid, None)
    n = len(samples)
    if n == 0:
        return (), ()
    times, vals = _prepare(series)
    first = bisect_left(times, samples[0])
    times, vals = times[first:], vals[first:]

    values: list[float | None] = [None] * n
    missing: list[bool] = [True] * n
    for i, t in enumerate(samples):
        j = bisect_right(times, t) - 1  # Latest steps point at or before t.
        if j >= 0 and t < times[j] + STEPS_VALUE_SPAN:
            values[i] = vals[j]
            missing[i] = False
    return tuple(values), tuple(missing)


# ---------------------------------------------------------------------------- Stages


def assign_stages(
    segments: Iterable[Stage_Segment], grid: Sequence[datetime]
) -> tuple[Sleep_Stage, ...]:
    """Sleep_Stage per sample from the half-open segment containing it (Requirement 10.11).

    Samples covered by no segment get ``Sleep_Stage.unknown``; stages are never
    interpolated. Segments must not overlap (``build_stage_segments``
    guarantees this); overlapping input raises ``ValueError``.
    """
    samples = _check_grid(grid, None)
    ordered = sorted(
        ((_to_utc(s.start_time), _to_utc(s.end_time), s.stage) for s in segments),
        key=lambda s: s[0],
    )
    for (_, prev_end, _), (next_start, _, _) in zip(ordered, ordered[1:]):
        if next_start < prev_end:
            raise ValueError("Stage_Segments must not overlap")
    starts = [s[0] for s in ordered]
    stages: list[Sleep_Stage] = []
    for t in samples:
        j = bisect_right(starts, t) - 1  # Latest segment starting at or before t.
        if j >= 0 and t < ordered[j][1]:
            stages.append(ordered[j][2])
        else:
            stages.append(Sleep_Stage.unknown)
    return tuple(stages)
