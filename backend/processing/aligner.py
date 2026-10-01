"""Aligner: SleepSession -> (Aligned_Timeline, Processing_Report).

Requirements 9.5, 9.8, 9.9, 10.1-10.18, 16.4. A pure function composed from
the resampling primitives in :mod:`backend.processing.resample`:

1. **Units (10.3, 10.16).** Every TelemetryPoint whose unit cannot be
   converted to its metric's Canonical_Unit is excluded, with one warning per
   ``(source, metric, unit)`` naming the excluded count. The rest are
   converted to the Canonical_Unit.
2. **UTC (9.5).** Timestamps become UTC instants; points outside the closed
   session range ``[start_time, end_time]`` are dropped (session telemetry is
   attached with that range, Requirement 8.10, so normally none are).
3. **Sort and deduplicate (10.1, 10.2).** Points are grouped per
   ``(source, metric)`` and sorted by timestamp; points sharing a timestamp
   collapse to the arithmetic mean of their canonical values (``math.fsum``,
   so the result does not depend on input order). Removed counts are recorded
   in ``Processing_Report.removed_duplicates`` keyed ``(source, metric)`` with
   the metric's string value, only for pairs that had duplicates.
4. **Sensor selection (10.13).** For temperature, humidity, and pressure with
   more than one source, only the source with the most (deduplicated,
   in-session) points is used; ties go to the lexicographically first source.
   A ``sensor_selection`` entry and a warning name the metric, the selected
   source, and the ignored sources.
   For the other metrics (heart rate, HRV, steps), points from several
   sources are combined; a timestamp present in more than one source takes
   the mean of the per-source values. This case does not arise with the MVP
   importers and is not reported.
5. **Resample (10.4-10.12, 10.15, 10.17).** Timeline_Resolution, sample grid,
   per-interval means / interpolation / gaps for Continuous_Metrics, steps
   hold, and per-sample Sleep_Stage.
6. **Report (10.9, 10.10, 16.4, 9.9).** Every metric without an in-session
   measured value is listed in ``unavailable_metrics`` with a warning. Every
   gap goes to ``gaps`` (see :meth:`Gap.to_report_entry`), plus one warning
   per metric stating the gap count and total gap duration. When fewer than
   50% of the samples have a heart_rate value and ``nearby_heart_rate``
   contains a heart_rate point within 14 h before start_time or 14 h after
   end_time (inclusive), a likely Source_Timezone mismatch warning is added;
   processing continues (9.9).

Determinism (10.18, 9.5): every step groups, sorts, or uses order-free
reductions, and all timestamps are compared and reported as UTC, so the result
is identical for any input order and any Display_Timezone or UTC offsets the
inputs carry. Warnings appear in a fixed order: unit exclusions (sorted by
source, metric, unit), sensor selection, unavailable metrics, gaps (each in
:class:`Metric` order), then the heart_rate timezone warning.

Imports only ``backend.domain``, other ``backend.processing`` modules, and the
standard library (Dependency_Rules, Requirement 17.2).
"""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Iterable

from backend.domain.errors import NO_ACTION_REQUIRED, Processing_Report
from backend.domain.session import SleepSession
from backend.domain.telemetry import CONTINUOUS_METRICS, Metric, TelemetryPoint
from backend.domain.timeline import MAX_INTERPOLATION_GAP, Aligned_Timeline
from backend.domain.timezones import DEFAULT_SOURCE_TIMEZONES, FITBIT_HEART_RATE
from backend.domain.units import is_convertible, to_canonical
from backend.processing.resample import (
    Gap,
    assign_stages,
    choose_resolution,
    resample_continuous,
    resample_steps,
    sample_grid,
)
from backend.processing.timezones import to_utc

__all__ = [
    "ENVIRONMENTAL_METRICS",
    "HEART_RATE_COVERAGE_THRESHOLD",
    "NEARBY_HEART_RATE_WINDOW",
    "align",
    "normalize_series",
]

#: Metrics subject to single-sensor selection (Requirement 10.13).
ENVIRONMENTAL_METRICS: tuple[Metric, ...] = (
    Metric.temperature,
    Metric.humidity,
    Metric.pressure,
)

#: Below this heart_rate sample coverage the timezone-mismatch check runs (9.9).
HEART_RATE_COVERAGE_THRESHOLD = 0.5

#: How far before start_time / after end_time nearby heart_rate points count (9.9).
NEARBY_HEART_RATE_WINDOW = timedelta(hours=14)

_SeriesMap = dict[Metric, list[tuple[datetime, float]]]


# --------------------------------------------------------------------------- helpers


def _metric_name(metric: object) -> str:
    return str(getattr(metric, "value", metric))


def _mean(values: list[float]) -> float:
    """Order-independent arithmetic mean, clamped to the inputs' range."""
    if len(values) == 1:
        return values[0]
    mean = math.fsum(values) / len(values)
    return min(max(mean, min(values)), max(values))


def _format_duration(td: timedelta) -> str:
    """Plain-language duration, e.g. ``1 h 5 min 30 s``."""
    total = int(round(td.total_seconds()))
    hours, rest = divmod(total, 3600)
    minutes, seconds = divmod(rest, 60)
    parts = []
    if hours:
        parts.append(f"{hours} h")
    if minutes:
        parts.append(f"{minutes} min")
    if seconds or not parts:
        parts.append(f"{seconds} s")
    return " ".join(parts)


def _format_minutes(td: timedelta) -> str:
    minutes = td.total_seconds() / 60
    return f"{minutes:g} minute" + ("" if minutes == 1 else "s")


def _plural(count: int, word: str) -> str:
    return f"{count} {word}" + ("" if count == 1 else "s")


# ------------------------------------------------------------------- normalization


def normalize_series(
    session: SleepSession, report: Processing_Report | None = None
) -> _SeriesMap:
    """Steps 1-4 of the Aligner: the in-session, canonical-unit, UTC, sorted,
    deduplicated, sensor-selected series of every metric (every :class:`Metric`
    key present; empty lists for metrics without data).

    Records unit exclusions, removed duplicates, and sensor selection in
    ``report`` when given.
    """
    report = report if report is not None else Processing_Report()
    start = to_utc(session.start_time)
    end = to_utc(session.end_time)

    # 1. Unit conversion / exclusion (10.3, 10.16); 2. UTC and session range (9.5).
    excluded: dict[tuple[str, str, str], int] = defaultdict(int)
    grouped: dict[tuple[str, Metric], dict[datetime, list[float]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for point in session.telemetry:
        if not is_convertible(point.metric, point.unit):
            excluded[(str(point.source), _metric_name(point.metric), str(point.unit))] += 1
            continue
        ts = to_utc(point.timestamp)
        if ts < start or ts > end:
            continue
        value = to_canonical(point.metric, point.value, point.unit)
        grouped[(point.source, point.metric)][ts].append(value)

    for (source, metric, unit), count in sorted(excluded.items()):
        report.add_warning(
            f"{_plural(count, 'measurement')} of {metric} from {source} "
            f"used the unit '{unit}', which cannot be converted to the standard unit "
            f"for {metric}. These measurements were left out of the replay.",
            subject=metric,
            recommended_action=(
                "Check the unit in the source file, correct it if possible, and import the file again."
            ),
        )

    # 3. Sort and collapse same-(source, metric, timestamp) duplicates (10.1, 10.2).
    per_source: dict[Metric, dict[str, list[tuple[datetime, float]]]] = defaultdict(dict)
    for key in sorted(grouped, key=lambda k: (k[0], k[1].value)):
        source, metric = key
        by_time = grouped[key]
        removed = 0
        series: list[tuple[datetime, float]] = []
        for ts in sorted(by_time):
            values = sorted(by_time[ts])
            removed += len(values) - 1
            series.append((ts, _mean(values)))
        if removed:
            report.removed_duplicates[(source, metric.value)] = removed
        per_source[metric][source] = series

    # 4. Sensor selection (10.13) / multi-source combination.
    result: _SeriesMap = {}
    for metric in Metric:
        sources = per_source.get(metric, {})
        if not sources:
            result[metric] = []
        elif len(sources) == 1:
            result[metric] = next(iter(sources.values()))
        elif metric in ENVIRONMENTAL_METRICS:
            selected = min(sources, key=lambda s: (-len(sources[s]), s))
            ignored = sorted(s for s in sources if s != selected)
            result[metric] = sources[selected]
            report.sensor_selection.append(
                {"metric": metric.value, "selected": selected, "ignored": ignored}
            )
            report.add_warning(
                f"{metric.value} data came from more than one sensor. Using {selected} "
                f"({_plural(len(sources[selected]), 'measurement')}) and ignoring "
                f"{', '.join(ignored)}.",
                subject=metric.value,
                recommended_action=(
                    "No action is required. To use a different sensor, import only that sensor's data."
                ),
            )
        else:
            combined: dict[datetime, list[float]] = defaultdict(list)
            for source in sorted(sources):
                for ts, value in sources[source]:
                    combined[ts].append(value)
            result[metric] = [(ts, _mean(sorted(combined[ts]))) for ts in sorted(combined)]
    return result


# --------------------------------------------------------------------------- align


def _has_nearby_heart_rate(
    nearby: Iterable[TelemetryPoint], start: datetime, end: datetime
) -> bool:
    before = start - NEARBY_HEART_RATE_WINDOW
    after = end + NEARBY_HEART_RATE_WINDOW
    for point in nearby:
        if point.metric != Metric.heart_rate:
            continue
        ts = point.timestamp
        if not isinstance(ts, datetime) or ts.tzinfo is None or ts.utcoffset() is None:
            continue
        ts = ts.astimezone(timezone.utc)
        if before <= ts < start or end < ts <= after:
            return True
    return False


def align(
    session: SleepSession,
    nearby_heart_rate: Iterable[TelemetryPoint] = (),
    *,
    heart_rate_source_timezone: str | None = None,
) -> tuple[Aligned_Timeline, Processing_Report]:
    """Align ``session`` onto a regular timeline (Requirements 9.5, 9.8, 9.9, 10).

    Args:
        session: The selected SleepSession, with its attached telemetry
            (values in source units) and Stage_Segments.
        nearby_heart_rate: heart_rate TelemetryPoints from all imports, used
            only for the Source_Timezone mismatch check (9.9). Points of other
            metrics, and points inside the session, are ignored.
        heart_rate_source_timezone: IANA name of the Source_Timezone applied
            to heart_rate files, quoted in the mismatch warning. Defaults to
            the Fitbit heart_rate default (UTC).

    Returns:
        ``(Aligned_Timeline, Processing_Report)``. The timeline has every
        :class:`Metric` key, values in Canonical_Units, ``None`` exactly where
        the Missing_Data_Mask is true, and UTC start/end/sample timestamps.

    Raises:
        ValueError: a naive timestamp, or ``end_time <= start_time``.
    """
    nearby = tuple(nearby_heart_rate)
    report = Processing_Report()
    start = to_utc(session.start_time)
    end = to_utc(session.end_time)

    series = normalize_series(session, report)

    # 5. Resolution, grid, resampling, stages (10.4-10.12, 10.15, 10.17).
    resolution = choose_resolution({m: s for m, s in series.items() if m in CONTINUOUS_METRICS})
    grid = sample_grid(start, end, resolution)
    values: dict[Metric, tuple[float | None, ...]] = {}
    missing: dict[Metric, tuple[bool, ...]] = {}
    gaps_by_metric: dict[Metric, tuple[Gap, ...]] = {}
    for metric in Metric:
        if metric in CONTINUOUS_METRICS:
            vals, mask, gaps = resample_continuous(metric, series[metric], grid, end)
            gaps_by_metric[metric] = gaps
        else:
            vals, mask = resample_steps(series[metric], grid)
        values[metric] = vals
        missing[metric] = mask
    stages = assign_stages(session.stages, grid)

    timeline = Aligned_Timeline(
        start_time=start,
        end_time=end,
        resolution_s=resolution,
        timestamps=grid,
        values=values,
        missing=missing,
        stages=stages,
    )

    # 6. Unavailable metrics (10.10, 16.4).
    for metric in Metric:
        if not series[metric]:
            report.unavailable_metrics.append(metric.value)
            report.add_warning(
                f"No {metric.value} data was found for this sleep session, so the replay "
                f"has no {metric.value} information.",
                subject=metric.value,
                recommended_action=(
                    f"If you have {metric.value} data for this night, import it and generate the replay again."
                ),
            )

    # Gaps: one entry per gap (10.9) and one warning per metric (16.4).
    for metric in Metric:
        gaps = gaps_by_metric.get(metric, ())
        if not gaps:
            continue
        report.gaps.extend(g.to_report_entry() for g in gaps)
        total = sum((g.elapsed for g in gaps), timedelta(0))
        report.add_warning(
            f"{metric.value} has {_plural(len(gaps), 'gap')} longer than "
            f"{_format_minutes(MAX_INTERPOLATION_GAP[metric])}, totaling "
            f"{_format_duration(total)}. Those parts of the timeline are marked as missing.",
            subject=metric.value,
            recommended_action=NO_ACTION_REQUIRED,
        )

    # Likely heart_rate Source_Timezone mismatch (9.9).
    n = timeline.sample_count
    covered = sum(1 for m in missing[Metric.heart_rate] if not m)
    coverage = covered / n
    if coverage < HEART_RATE_COVERAGE_THRESHOLD and _has_nearby_heart_rate(nearby, start, end):
        tz_name = heart_rate_source_timezone or DEFAULT_SOURCE_TIMEZONES[FITBIT_HEART_RATE]
        report.add_warning(
            f"Only {coverage * 100:.1f}% of this sleep session has heart_rate data, but heart "
            f"rate data exists within 14 hours of the session. The Source_Timezone applied to "
            f"heart_rate files ({tz_name}) is likely incorrect.",
            subject=Metric.heart_rate.value,
            recommended_action=(
                "Import the heart_rate files again and choose a different Source_Timezone for "
                "the heart_rate file type (for example your local timezone)."
            ),
        )

    return timeline, report
