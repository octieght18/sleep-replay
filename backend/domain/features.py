"""Feature_Series, Feature_Window, Metric_Window_Features, and Coarse_State.

Requirements 12.1–12.12 and the Default Parameter Values table
(Feature_Window_Span, Minimum_State_Duration, smoothing windows). Imports only
the standard library and other ``backend.domain`` modules (Dependency_Rules,
Requirement 17.2).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from types import MappingProxyType
from typing import Mapping, TypeAlias

from backend.domain.stages import Sleep_Stage
from backend.domain.telemetry import CONTINUOUS_METRICS, Metric

__all__ = [
    "FEATURE_WINDOW_SPAN_S",
    "MINIMUM_STATE_DURATION_S",
    "SMOOTHING_WINDOW",
    "STAGE_FRACTION_TOLERANCE",
    "feature_window_count",
    "Metric_Window_Features",
    "Feature_Window",
    "Feature_Series",
    "Coarse_State",
]

#: Replay_Time span of one Feature_Window, in seconds (Requirement 12.1).
FEATURE_WINDOW_SPAN_S: float = 0.5

#: Shortest Replay_Time run of one Coarse_State, in seconds (Requirement 12.9, 12.12).
MINIMUM_STATE_DURATION_S: float = 1.0

#: Night_Time smoothing window per Continuous_Metric, centered on the
#: Feature_Window midpoint (Requirement 12.4).
SMOOTHING_WINDOW: Mapping[Metric, timedelta] = MappingProxyType(
    {
        Metric.heart_rate: timedelta(minutes=5),
        Metric.hrv_rmssd: timedelta(minutes=15),
        Metric.temperature: timedelta(minutes=15),
        Metric.humidity: timedelta(minutes=15),
        Metric.pressure: timedelta(minutes=15),
    }
)

#: Allowed deviation of the stage-fraction sum from 1.0 (Requirement 12.11).
STAGE_FRACTION_TOLERANCE: float = 1e-6

#: The smoothed Sleep_Stage of one Feature_Window (glossary: Coarse_State).
#: A Coarse_State sequence has one entry per Feature_Window (Requirement 12.12).
Coarse_State: TypeAlias = Sleep_Stage


def feature_window_count(
    target_duration_s: float, span_s: float = FEATURE_WINDOW_SPAN_S
) -> int:
    """Feature_Window count: ceil(Target_Duration / Feature_Window_Span) (Req 12.10)."""
    if not (target_duration_s > 0 and span_s > 0):
        raise ValueError("target_duration_s and span_s must be positive")
    return math.ceil(target_duration_s / span_s)


def _is_finite_number(value: object) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
    )


def _check_optional_number(value: object, name: str) -> None:
    if value is not None and not _is_finite_number(value):
        raise ValueError(f"{name} must be a finite number or None")


def _check_unit_interval(value: object, name: str) -> None:
    if not _is_finite_number(value) or not 0.0 <= value <= 1.0:
        raise ValueError(f"{name} must lie in [0.0, 1.0], got {value!r}")


@dataclass(frozen=True)
class Metric_Window_Features:
    """Features of one Continuous_Metric in one Feature_Window (Requirement 12.2–12.5).

    ``None`` marks a missing feature. ``mean``, ``minimum``, and ``maximum`` are
    either all present (with ``minimum <= mean <= maximum``) or all missing; when
    all are missing, ``missing_fraction`` is 1.0 (Req 12.3). ``slope`` is in
    Canonical_Unit per hour of Night_Time. ``smoothed`` is independent of the
    window's own samples (Req 12.4, 12.5).
    """

    mean: float | None
    minimum: float | None
    maximum: float | None
    slope: float | None
    missing_fraction: float
    smoothed: float | None

    def __post_init__(self) -> None:
        for name in ("mean", "minimum", "maximum", "slope", "smoothed"):
            _check_optional_number(getattr(self, name), name)
        _check_unit_interval(self.missing_fraction, "missing_fraction")
        present = [v is not None for v in (self.mean, self.minimum, self.maximum)]
        if any(present) and not all(present):
            raise ValueError("mean, minimum, and maximum must be all present or all missing")
        if all(present):
            if not self.minimum <= self.mean <= self.maximum:
                raise ValueError("mean must lie between minimum and maximum")
        else:
            if self.slope is not None:
                raise ValueError("slope must be missing when mean is missing")
            if self.missing_fraction != 1.0:
                raise ValueError("missing_fraction must be 1.0 when no sample is present")

    @property
    def has_values(self) -> bool:
        """True when the window has at least one non-missing sample of the metric."""
        return self.mean is not None


@dataclass(frozen=True)
class Feature_Window:
    """One contiguous span of Night_Time mapping to one Feature_Window_Span of Replay_Time.

    - ``start_time`` / ``end_time``: Night_Time bounds, ``start_time < end_time`` (Req 12.1).
    - ``per_metric``: features per Continuous_Metric (Req 12.2).
    - ``stage_fractions``: fraction of Night_Time per Sleep_Stage; stages absent
      from the mapping have fraction 0.0; the sum is 1.0 within
      :data:`STAGE_FRACTION_TOLERANCE` (Req 12.6, 12.11).
    - ``dominant_stage``: largest fraction, ties by ``DOMINANT_STAGE_ORDER`` (Req 12.6).
    - ``movement_intensity``: Movement_Intensity in [0.0, 1.0] (Req 12.7, 12.8).

    Mappings are copied on construction.
    """

    start_time: datetime
    end_time: datetime
    per_metric: Mapping[Metric, Metric_Window_Features] = field(hash=False)
    stage_fractions: Mapping[Sleep_Stage, float] = field(hash=False)
    dominant_stage: Sleep_Stage
    movement_intensity: float

    def __post_init__(self) -> None:
        for name in ("start_time", "end_time"):
            value = getattr(self, name)
            if not isinstance(value, datetime) or value.utcoffset() is None:
                raise ValueError(f"{name} must be a timezone-aware datetime")
        if self.end_time <= self.start_time:
            raise ValueError("end_time must be later than start_time")

        per_metric = dict(self.per_metric)
        for metric, features in per_metric.items():
            if metric not in CONTINUOUS_METRICS:
                raise ValueError(f"per_metric keys must be Continuous_Metrics, got {metric!r}")
            if not isinstance(features, Metric_Window_Features):
                raise TypeError(f"per_metric[{metric.value}] must be Metric_Window_Features")
        object.__setattr__(self, "per_metric", per_metric)

        fractions = dict(self.stage_fractions)
        for stage, fraction in fractions.items():
            if not isinstance(stage, Sleep_Stage):
                raise TypeError(f"stage_fractions keys must be Sleep_Stage, got {stage!r}")
            _check_unit_interval(fraction, f"stage_fractions[{stage.value}]")
        if abs(math.fsum(fractions.values()) - 1.0) > STAGE_FRACTION_TOLERANCE:
            raise ValueError("stage_fractions must sum to 1.0")
        object.__setattr__(self, "stage_fractions", fractions)

        if not isinstance(self.dominant_stage, Sleep_Stage):
            raise TypeError("dominant_stage must be a Sleep_Stage")
        _check_unit_interval(self.movement_intensity, "movement_intensity")

    @property
    def midpoint(self) -> datetime:
        """Night_Time midpoint, the center of the smoothing interval (Req 12.4)."""
        return self.start_time + (self.end_time - self.start_time) / 2

    def stage_fraction(self, stage: Sleep_Stage) -> float:
        """Fraction of the window's Night_Time covered by ``stage`` (0.0 when absent)."""
        return self.stage_fractions.get(stage, 0.0)


@dataclass(frozen=True)
class Feature_Series:
    """The per-Feature_Window features of one SleepSession (Requirement 12).

    ``windows`` are chronological, contiguous, and non-overlapping (each starts
    where the previous ends, Req 12.10). ``compression_ratio`` is the
    SleepSession duration divided by the Target_Duration (Req 11.3).
    """

    windows: tuple[Feature_Window, ...]
    compression_ratio: float
    session_hrv: float | None = None
    brief_awakenings: tuple[tuple[datetime, datetime], ...] = ()
    unavailable_metrics: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _check_optional_number(self.session_hrv, "session_hrv")
        windows = tuple(self.windows)
        if not all(isinstance(w, Feature_Window) for w in windows):
            raise TypeError("windows must contain only Feature_Window values")
        for i in range(1, len(windows)):
            if windows[i].start_time != windows[i - 1].end_time:
                raise ValueError(f"windows[{i}] does not start where windows[{i - 1}] ends")
        object.__setattr__(self, "windows", windows)
        if not _is_finite_number(self.compression_ratio) or self.compression_ratio <= 0:
            raise ValueError("compression_ratio must be a positive finite number")

    def __len__(self) -> int:
        return len(self.windows)

    @property
    def dominant_stages(self) -> tuple[Sleep_Stage, ...]:
        """Dominant Sleep_Stage of each window, in order (input to Coarse_State smoothing)."""
        return tuple(w.dominant_stage for w in self.windows)
