"""Feature_Extractor: Feature_Windows, per-metric statistics, stages, movement, Coarse_States.

Requirement 12. This module is built up in three steps (tasks 13.1-13.3):

* **13.1 (this code): windows and per-metric statistics.**
  :func:`feature_window_bounds` splits the SleepSession into
  ``ceil(Target_Duration / Feature_Window_Span)`` consecutive windows
  (Req 12.1, 12.10) and :func:`metric_window_features` computes the
  per-Continuous_Metric statistics of each window (Req 12.2-12.5).
  :func:`assemble_feature_series` builds the domain :class:`Feature_Series`
  from those results plus per-window stage data and Movement_Intensity, which
  the caller supplies explicitly.
* **13.2: stage fractions and Movement_Intensity** (Req 12.6-12.8, 12.11).
  :func:`window_stage_coverage` and :func:`window_movement_intensity` compute
  these per :class:`Window_Bounds`; the results are passed to
  :func:`assemble_feature_series`. No placeholder values exist in this module:
  a ``Feature_Window`` is only ever constructed from real stage fractions,
  dominant stage, and Movement_Intensity.
* **13.3: Coarse_State smoothing and** ``extract(timeline, session, target_duration)``
  (Req 12.9, 12.12), which ties the three steps together.

Window layout (Req 12.1, 12.10)
-------------------------------
Window ``i`` spans the Night_Time ``[night_time(i * 0.5 s), night_time((i + 1) * 0.5 s))``
of the session's :class:`Night_Compression`, i.e. ``Compression_Ratio x 0.5 s``
of Night_Time. The last window ends exactly at the session ``end_time``.
Consecutive windows share their boundary instant, so they cover the session
without gaps or overlaps. A window holds the Aligned_Timeline samples with
timestamp ``start <= t < end``; the last window also holds a sample at
exactly ``end_time`` (the Aligner never emits one, Req 10.6, but the rule is
applied anyway).

Per-metric statistics (Req 12.2-12.5)
-------------------------------------
For each Continuous_Metric and window, over the samples not marked missing:

* ``mean``, ``minimum``, ``maximum``. The mean is clamped into
  ``[minimum, maximum]`` so float rounding can never break the invariant
  (Req 12.11).
* ``slope``: least-squares slope of value against Night_Time in
  Canonical_Unit per hour. It needs at least two non-missing samples; with
  exactly one, the fit is undefined and ``slope`` is ``None`` while
  ``mean``/``minimum``/``maximum`` are present.
* ``missing_fraction``: missing sample count / window sample count. A metric
  absent from the timeline counts as missing everywhere. When no non-missing
  sample exists (including an empty window) all four statistics are ``None``
  and the fraction is 1.0 (Req 12.3).
* ``smoothed``: mean of the non-missing samples with timestamp in the closed
  interval ``[midpoint - w/2, midpoint + w/2]`` intersected with
  ``[start_time, end_time]``, where ``w`` is the metric's
  :data:`~backend.domain.features.SMOOTHING_WINDOW`. ``None`` when that
  interval holds no non-missing sample (Req 12.4, 12.5).

Stage fractions and dominant stage (Req 12.6, 12.11)
----------------------------------------------------
Stage fractions come from the SleepSession's **Stage_Segments**, not from the
Aligned_Timeline's per-sample stages. Requirement 12.6 asks for the fraction
of the window's *Night_Time* covered by each stage with Brief_Awakening
precedence; per-sample stages quantize time to the Timeline_Resolution, give
a zero-sample window no stage at all, and do not carry the Brief_Awakening
flag that the movement fallback (Req 12.8) needs. The segments are first
normalized with :func:`~backend.domain.stages.build_stage_segments` (clip to
the session, Brief_Awakening wins overlaps, Req 2.8), which leaves
already-normalized segments unchanged.

Coverage is measured in exact integer microseconds of UTC Night_Time. Time
covered by no segment, or by an ``unknown`` segment, counts as ``unknown``,
computed as the remainder of the window length so the integer durations sum
exactly to the window length; each fraction is ``duration / window length``
clamped to ``[0, 1]``, so the float sum is 1.0 within
:data:`~backend.domain.features.STAGE_FRACTION_TOLERANCE`. Every Sleep_Stage
has an entry (0.0 when absent). The dominant stage is the largest duration,
ties broken by :data:`~backend.domain.stages.DOMINANT_STAGE_ORDER`
(compared on the exact integer durations, so float rounding cannot split a tie).

Movement_Intensity (Req 12.7, 12.8, 12.11)
------------------------------------------
When the Aligned_Timeline has at least one non-missing steps sample, each
window's Movement_Intensity is::

    min(1.0, max(0.0, mean_steps / MOVEMENT_FULL_SCALE))

where ``mean_steps`` is the mean steps per minute of the window's non-missing
steps samples and :data:`MOVEMENT_FULL_SCALE` is 30 steps per minute. The
map is monotone non-decreasing (IEEE division by a positive constant is
monotone), 0 steps per minute gives 0.0, and a window without a non-missing
steps sample gives 0.0. When the session has no non-missing steps sample at
all, each window's Movement_Intensity is instead the fraction of its
Night_Time covered by restless Stage_Segments or Brief_Awakenings, and a
warning (:data:`STAGE_DERIVED_MOVEMENT_WARNING`) is added to the
Processing_Report.

All times are UTC instants; offsets are exact integer microseconds from the
session start. Imports only the standard library, numpy, ``backend.domain``,
and this package.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

import numpy as np

from backend.domain.errors import Processing_Report
from backend.domain.features import (
    FEATURE_WINDOW_SPAN_S,
    SMOOTHING_WINDOW,
    Feature_Series,
    Feature_Window,
    Metric_Window_Features,
    feature_window_count,
)
from backend.domain.session import SleepSession
from backend.domain.stages import (
    DOMINANT_STAGE_ORDER,
    Sleep_Stage,
    Stage_Interval,
    Stage_Segment,
    build_stage_segments,
)
from backend.domain.telemetry import CONTINUOUS_METRICS, Metric
from backend.domain.timeline import Aligned_Timeline
from backend.processing.compression import Night_Compression
from backend.processing.timezones import to_utc

__all__ = [
    "FEATURE_METRICS",
    "MOVEMENT_FULL_SCALE",
    "STAGE_DERIVED_MOVEMENT_WARNING",
    "Window_Bounds",
    "Window_Stage_Coverage",
    "feature_window_bounds",
    "metric_window_features",
    "dominant_stage",
    "window_stage_coverage",
    "session_has_steps",
    "window_mean_steps",
    "movement_intensity_from_mean",
    "window_movement_intensity",
    "assemble_feature_series",
]

#: Continuous_Metrics with per-window statistics, in :class:`Metric` declaration order.
FEATURE_METRICS: tuple[Metric, ...] = tuple(m for m in Metric if m in CONTINUOUS_METRICS)

#: Mean steps per minute at which Movement_Intensity reaches 1.0 (Req 12.7).
#: Chosen for sleep: a few steps per minute registers as slight movement,
#: 9 steps/min reaches Restless_Threshold (0.3), 18 steps/min reaches
#: Movement_Burst_Threshold (0.6), and 30 or more is full-scale movement.
MOVEMENT_FULL_SCALE: float = 30.0

#: Processing_Report warning when movement comes from sleep stages (Req 12.8).
STAGE_DERIVED_MOVEMENT_WARNING: str = (
    "Movement was derived from sleep stages (restless segments and brief "
    "awakenings) because the session has no steps data."
)

_ONE_US = timedelta(microseconds=1)
_US_PER_HOUR = 3_600 * 1_000_000


@dataclass(frozen=True)
class Window_Bounds:  # noqa: N801 - name follows the spec glossary
    """Night_Time bounds and sample range of one Feature_Window (Req 12.1).

    Attributes:
        index: Position of the window, from 0.
        start_time: Window start (UTC, inclusive).
        end_time: Window end (UTC, exclusive except for the last window).
        start_us: ``start_time`` as microseconds after the session start.
        end_us: ``end_time`` as microseconds after the session start.
        sample_start: First Aligned_Timeline sample index in the window.
        sample_stop: One past the last sample index (``sample_start == sample_stop``
            for a window with no samples).
    """

    index: int
    start_time: datetime
    end_time: datetime
    start_us: int
    end_us: int
    sample_start: int
    sample_stop: int

    @property
    def sample_count(self) -> int:
        """Number of Aligned_Timeline samples in the window."""
        return self.sample_stop - self.sample_start

    @property
    def midpoint_us(self) -> float:
        """Midpoint in microseconds after the session start (centre of smoothing, Req 12.4)."""
        return (self.start_us + self.end_us) / 2


def _offsets_us(timeline: Aligned_Timeline, origin: datetime) -> list[int]:
    return [(to_utc(t) - origin) // _ONE_US for t in timeline.timestamps]


def _check_same_session(timeline: Aligned_Timeline, compression: Night_Compression) -> None:
    if (
        to_utc(timeline.start_time) != compression.start_time
        or to_utc(timeline.end_time) != compression.end_time
    ):
        raise ValueError("the Aligned_Timeline and Night_Compression cover different sessions")


def feature_window_bounds(
    timeline: Aligned_Timeline, compression: Night_Compression
) -> tuple[Window_Bounds, ...]:
    """Split the session into ``ceil(Target_Duration / 0.5 s)`` Feature_Windows (Req 12.1, 12.10).

    Raises:
        ValueError: ``timeline`` and ``compression`` have different session bounds.
    """
    _check_same_session(timeline, compression)
    origin = compression.start_time
    count = feature_window_count(compression.target_duration_s, FEATURE_WINDOW_SPAN_S)
    target = compression.target_duration_s

    # Shared boundary instants; the last one is pinned to end_time.
    edges = [compression.night_time(min(i * FEATURE_WINDOW_SPAN_S, target)) for i in range(count)]
    edges.append(compression.end_time)

    offsets = _offsets_us(timeline, origin)
    windows: list[Window_Bounds] = []
    for i in range(count):
        start, end = edges[i], edges[i + 1]
        start_us = (start - origin) // _ONE_US
        end_us = (end - origin) // _ONE_US
        lo = bisect_left(offsets, start_us)
        is_last = i == count - 1
        hi = bisect_right(offsets, end_us) if is_last else bisect_left(offsets, end_us)
        windows.append(Window_Bounds(i, start, end, start_us, end_us, lo, hi))
    return tuple(windows)


def _metric_arrays(timeline: Aligned_Timeline, metric: Metric) -> np.ndarray:
    """Per-sample values as float64 with NaN wherever the sample is missing."""
    n = timeline.sample_count
    values = timeline.values.get(metric)
    mask = timeline.missing.get(metric)
    if values is None or mask is None:
        return np.full(n, np.nan)
    return np.array(
        [np.nan if m else float(v) for v, m in zip(values, mask)], dtype=np.float64
    )


def _slope_per_hour(t_us: np.ndarray, y: np.ndarray) -> float | None:
    """Least-squares slope of ``y`` against time, in units per hour; ``None`` if undefined."""
    if y.size < 2:
        return None
    x = (t_us - t_us.mean()) / _US_PER_HOUR
    denom = float(np.dot(x, x))
    if denom <= 0.0:
        return None
    slope = float(np.dot(x, y - y.mean())) / denom
    return slope if np.isfinite(slope) else None


def _window_stats(
    t_us: np.ndarray, y: np.ndarray, sample_count: int, smoothed: float | None
) -> Metric_Window_Features:
    present = ~np.isnan(y)
    n_present = int(present.sum())
    if n_present == 0:
        return Metric_Window_Features(None, None, None, None, 1.0, smoothed)
    vals = y[present]
    lo = float(vals.min())
    hi = float(vals.max())
    mean = min(max(float(vals.mean()), lo), hi)
    slope = _slope_per_hour(t_us[present], vals)
    missing_fraction = (sample_count - n_present) / sample_count
    return Metric_Window_Features(mean, lo, hi, slope, missing_fraction, smoothed)


def metric_window_features(
    timeline: Aligned_Timeline, windows: Sequence[Window_Bounds]
) -> tuple[dict[Metric, Metric_Window_Features], ...]:
    """Per-Continuous_Metric features of every window (Req 12.2-12.5).

    Returns one mapping per window (same order as ``windows``) with an entry
    for every metric in :data:`FEATURE_METRICS`.
    """
    origin = to_utc(timeline.start_time)
    session_end_us = (to_utc(timeline.end_time) - origin) // _ONE_US
    offsets = _offsets_us(timeline, origin)
    t_all = np.array(offsets, dtype=np.float64)

    result: list[dict[Metric, Metric_Window_Features]] = [{} for _ in windows]
    for metric in FEATURE_METRICS:
        y_all = _metric_arrays(timeline, metric)
        half_us = (SMOOTHING_WINDOW[metric] // _ONE_US) / 2
        for k, w in enumerate(windows):
            # Smoothing interval: closed, centred on the midpoint, truncated to the session.
            s_lo = max(0.0, w.midpoint_us - half_us)
            s_hi = min(float(session_end_us), w.midpoint_us + half_us)
            i_lo = bisect_left(offsets, s_lo)
            i_hi = bisect_right(offsets, s_hi)
            s_vals = y_all[i_lo:i_hi]
            s_vals = s_vals[~np.isnan(s_vals)]
            smoothed = float(s_vals.mean()) if s_vals.size else None

            sl = slice(w.sample_start, w.sample_stop)
            result[k][metric] = _window_stats(t_all[sl], y_all[sl], w.sample_count, smoothed)
    return tuple(result)


# ---------------------------------------------------------------------------
# Stage fractions and dominant stage (task 13.2, Req 12.6, 12.11)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Window_Stage_Coverage:  # noqa: N801 - name follows the spec glossary
    """Stage coverage of one Feature_Window's Night_Time (Req 12.6, 12.8).

    Attributes:
        stage_fractions: Fraction per Sleep_Stage, one entry for every stage
            (0.0 when absent); sums to 1.0 within STAGE_FRACTION_TOLERANCE.
        dominant_stage: Largest fraction, ties by ``DOMINANT_STAGE_ORDER``.
        movement_fraction: Fraction covered by restless Stage_Segments or
            Brief_Awakenings (the Req 12.8 fallback Movement_Intensity).
    """

    stage_fractions: Mapping[Sleep_Stage, float]
    dominant_stage: Sleep_Stage
    movement_fraction: float


def dominant_stage(amounts: Mapping[Sleep_Stage, float]) -> Sleep_Stage:
    """Stage with the largest amount; ties go to the earliest in ``DOMINANT_STAGE_ORDER``.

    ``amounts`` may hold fractions or durations; missing stages count as 0.
    An empty or all-zero mapping gives ``awake`` by the tie order, but the
    extractor never passes one (the window's stages always cover it).
    """
    best = DOMINANT_STAGE_ORDER[0]
    best_amount = amounts.get(best, 0)
    for stage in DOMINANT_STAGE_ORDER[1:]:
        amount = amounts.get(stage, 0)
        if amount > best_amount:  # strict: an equal later stage never wins
            best, best_amount = stage, amount
    return best


def _clamp_unit(value: float) -> float:
    return min(1.0, max(0.0, value))


def _normalized_segments(session: SleepSession) -> list[Stage_Segment]:
    """Session stages clipped to the session with Brief_Awakening precedence (Req 2.8, 12.6)."""
    return build_stage_segments(
        (
            Stage_Interval(s.start_time, s.end_time, s.stage, s.is_brief_awakening)
            for s in session.stages
        ),
        session.start_time,
        session.end_time,
    )


def window_stage_coverage(
    session: SleepSession, windows: Sequence[Window_Bounds]
) -> tuple[Window_Stage_Coverage, ...]:
    """Per-window stage fractions, dominant stage, and restless/Brief_Awakening coverage.

    Computed from the session's Stage_Segments over each window's Night_Time
    ``[start_time, end_time)`` in exact UTC microseconds (Req 12.6, 12.11).

    Raises:
        ValueError: a window has non-positive length.
    """
    origin = to_utc(session.start_time)

    def us(t: datetime) -> int:
        return (to_utc(t) - origin) // _ONE_US

    segments = [
        (us(s.start_time), us(s.end_time), s.stage, s.stage is Sleep_Stage.restless or s.is_brief_awakening)
        for s in _normalized_segments(session)
    ]  # sorted by start, non-overlapping

    result: list[Window_Stage_Coverage] = []
    j = 0  # first segment that may still overlap the current or a later window
    for w in windows:
        w_start, w_end = us(w.start_time), us(w.end_time)
        length = w_end - w_start
        if length <= 0:
            raise ValueError(f"window {w.index} has non-positive length")
        while j < len(segments) and segments[j][1] <= w_start:
            j += 1
        durations: dict[Sleep_Stage, int] = {stage: 0 for stage in DOMINANT_STAGE_ORDER}
        moving = 0
        k = j
        while k < len(segments) and segments[k][0] < w_end:
            s_start, s_end, stage, is_moving = segments[k]
            overlap = min(s_end, w_end) - max(s_start, w_start)
            if overlap > 0 and stage is not Sleep_Stage.unknown:
                durations[stage] += overlap
            if overlap > 0 and is_moving:
                moving += overlap
            k += 1
        # Uncovered time and unknown segments: the exact integer remainder.
        known = sum(d for s, d in durations.items() if s is not Sleep_Stage.unknown)
        durations[Sleep_Stage.unknown] = length - known
        fractions = {stage: _clamp_unit(d / length) for stage, d in durations.items()}
        result.append(
            Window_Stage_Coverage(
                stage_fractions=fractions,
                dominant_stage=dominant_stage(durations),
                movement_fraction=_clamp_unit(moving / length),
            )
        )
    return tuple(result)


# ---------------------------------------------------------------------------
# Movement_Intensity (task 13.2, Req 12.7, 12.8, 12.11)
# ---------------------------------------------------------------------------


def _steps_array(timeline: Aligned_Timeline) -> np.ndarray:
    return _metric_arrays(timeline, Metric.steps)


def session_has_steps(timeline: Aligned_Timeline) -> bool:
    """True when the Aligned_Timeline has at least one non-missing steps sample (Req 12.7)."""
    mask = timeline.missing.get(Metric.steps)
    return mask is not None and not all(mask)


def window_mean_steps(
    timeline: Aligned_Timeline, windows: Sequence[Window_Bounds]
) -> tuple[float | None, ...]:
    """Mean steps per minute of each window's non-missing steps samples; ``None`` when none."""
    steps = _steps_array(timeline)
    means: list[float | None] = []
    for w in windows:
        vals = steps[w.sample_start : w.sample_stop]
        vals = vals[~np.isnan(vals)]
        means.append(float(vals.mean()) if vals.size else None)
    return tuple(means)


def movement_intensity_from_mean(mean_steps_per_min: float | None) -> float:
    """Movement_Intensity ``min(1, max(0, mean / MOVEMENT_FULL_SCALE))`` (Req 12.7).

    Monotone non-decreasing in the mean; 0 steps per minute and ``None``
    (no non-missing steps sample) give 0.0; the result lies in ``[0, 1]``.
    """
    if mean_steps_per_min is None or not np.isfinite(mean_steps_per_min):
        return 0.0
    return _clamp_unit(mean_steps_per_min / MOVEMENT_FULL_SCALE)


def window_movement_intensity(
    timeline: Aligned_Timeline,
    windows: Sequence[Window_Bounds],
    coverage: Sequence[Window_Stage_Coverage],
    report: Processing_Report | None = None,
) -> tuple[float, ...]:
    """Movement_Intensity of every window (Req 12.7, 12.8).

    From mean steps per minute when the session has any non-missing steps
    sample; otherwise the restless/Brief_Awakening coverage fraction from
    ``coverage`` (one entry per window), adding
    :data:`STAGE_DERIVED_MOVEMENT_WARNING` to ``report`` when given.

    Raises:
        ValueError: ``coverage`` has a different length than ``windows``.
    """
    if len(coverage) != len(windows):
        raise ValueError(f"coverage has {len(coverage)} entries, expected {len(windows)}")
    if session_has_steps(timeline):
        return tuple(movement_intensity_from_mean(m) for m in window_mean_steps(timeline, windows))
    if report is not None:
        report.add_warning(STAGE_DERIVED_MOVEMENT_WARNING, subject=Metric.steps.value)
    return tuple(_clamp_unit(c.movement_fraction) for c in coverage)


def assemble_feature_series(
    windows: Sequence[Window_Bounds],
    per_metric: Sequence[Mapping[Metric, Metric_Window_Features]],
    stage_fractions: Sequence[Mapping[Sleep_Stage, float]],
    dominant_stages: Sequence[Sleep_Stage],
    movement_intensity: Sequence[float],
    compression_ratio: float,
) -> Feature_Series:
    """Build the :class:`Feature_Series` from per-window parts (Req 12).

    ``stage_fractions``, ``dominant_stages``, and ``movement_intensity`` come
    from the stage/movement step (task 13.2); every sequence must have one
    entry per window.

    Raises:
        ValueError: sequence lengths differ, or a part violates a
            ``Feature_Window`` / ``Feature_Series`` invariant.
    """
    n = len(windows)
    for name, seq in (
        ("per_metric", per_metric),
        ("stage_fractions", stage_fractions),
        ("dominant_stages", dominant_stages),
        ("movement_intensity", movement_intensity),
    ):
        if len(seq) != n:
            raise ValueError(f"{name} has {len(seq)} entries, expected {n}")
    return Feature_Series(
        windows=tuple(
            Feature_Window(
                start_time=w.start_time,
                end_time=w.end_time,
                per_metric=per_metric[k],
                stage_fractions=stage_fractions[k],
                dominant_stage=dominant_stages[k],
                movement_intensity=movement_intensity[k],
            )
            for k, w in enumerate(windows)
        ),
        compression_ratio=compression_ratio,
    )
