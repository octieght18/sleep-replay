"""Aligned_Timeline: the regular, resampled timeline of one SleepSession.

Requirements 10.4, 10.5, 10.6, 10.8, 10.9, 10.11, 10.12, 10.15 and the
Default Parameter Values table (Timeline_Resolution candidates,
Max_Interpolation_Gap). Imports only the standard library and other
``backend.domain`` modules (Dependency_Rules, Requirement 17.2).

All elapsed-time arithmetic is done on UTC instants, so a session crossing a
DST transition gets the correct number of samples (Requirement 10.15).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from types import MappingProxyType
from typing import Mapping

from backend.domain.stages import Sleep_Stage
from backend.domain.telemetry import Metric

__all__ = [
    "TIMELINE_RESOLUTION_CANDIDATES_S",
    "FALLBACK_TIMELINE_RESOLUTION_S",
    "MAX_INTERPOLATION_GAP",
    "STEPS_VALUE_SPAN",
    "sample_count",
    "sample_timestamps",
    "Aligned_Timeline",
]

#: Timeline_Resolution candidates in seconds, ascending (Default Parameter Values).
TIMELINE_RESOLUTION_CANDIDATES_S: tuple[int, ...] = (1, 5, 10, 15, 30, 60)

#: Resolution used when no Continuous_Metric has two points in the session, or
#: when the shortest median interval exceeds 60 s (Requirement 10.5).
FALLBACK_TIMELINE_RESOLUTION_S: int = 60

#: Longest gap between measured values across which the Aligner interpolates
#: (Max_Interpolation_Gap, Requirement 10.8). Continuous_Metrics only.
MAX_INTERPOLATION_GAP: Mapping[Metric, timedelta] = MappingProxyType(
    {
        Metric.heart_rate: timedelta(minutes=5),
        Metric.hrv_rmssd: timedelta(minutes=15),
        Metric.temperature: timedelta(minutes=15),
        Metric.humidity: timedelta(minutes=15),
        Metric.pressure: timedelta(minutes=15),
    }
)

#: A steps value applies to samples in [timestamp, timestamp + 60 s) (Requirement 10.12).
STEPS_VALUE_SPAN: timedelta = timedelta(seconds=60)


def _require_aware(value: datetime, name: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be a timezone-aware datetime")


def _check_resolution(resolution_s: int) -> None:
    if isinstance(resolution_s, bool) or resolution_s not in TIMELINE_RESOLUTION_CANDIDATES_S:
        raise ValueError(
            f"resolution_s must be one of {TIMELINE_RESOLUTION_CANDIDATES_S}, got {resolution_s!r}"
        )


def sample_count(start_time: datetime, end_time: datetime, resolution_s: int) -> int:
    """Number of Aligned_Timeline samples: ceil(elapsed duration / resolution) (Req 10.15).

    Elapsed time is measured between UTC instants. Raises ``ValueError`` when
    either bound is naive, ``end_time <= start_time``, or the resolution is not
    a Timeline_Resolution candidate.
    """
    _require_aware(start_time, "start_time")
    _require_aware(end_time, "end_time")
    _check_resolution(resolution_s)
    duration = end_time.astimezone(timezone.utc) - start_time.astimezone(timezone.utc)
    if duration <= timedelta(0):
        raise ValueError("end_time must be later than start_time")
    step = timedelta(seconds=resolution_s)
    # Exact integer ceiling on timedelta (microsecond precision, no float error).
    return -(-duration // step)


def sample_timestamps(
    start_time: datetime, end_time: datetime, resolution_s: int
) -> tuple[datetime, ...]:
    """Sample timestamps ``start + k * resolution`` strictly before ``end_time`` (Req 10.6).

    Timestamps are returned in UTC.
    """
    count = sample_count(start_time, end_time, resolution_s)
    start_utc = start_time.astimezone(timezone.utc)
    step = timedelta(seconds=resolution_s)
    return tuple(start_utc + k * step for k in range(count))


def _is_finite_number(value: object) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
    )


@dataclass(frozen=True)
class Aligned_Timeline:
    """A regular sequence of samples from session start_time to end_time.

    - ``resolution_s``: the Timeline_Resolution, one of
      :data:`TIMELINE_RESOLUTION_CANDIDATES_S` (Req 10.4, 10.5).
    - ``timestamps``: ``start_time + k * resolution_s`` for every k >= 0 earlier
      than ``end_time`` (Req 10.6); count ``ceil(duration / resolution)`` (Req 10.15).
    - ``values``: per-metric, per-sample value in the Canonical_Unit, ``None``
      exactly where the sample is marked missing.
    - ``missing``: the Missing_Data_Mask, per metric and sample (Req 10.9, 10.10, 10.17).
      ``values`` and ``missing`` have the same metric keys; the Aligner emits
      every :class:`Metric`, with all samples missing for unavailable metrics.
    - ``stages``: the Sleep_Stage of every sample (Req 10.11).

    Sample_Interval i is ``[timestamps[i], timestamps[i + 1])``; the last one
    ends at ``end_time``. The mappings are copied on construction; their
    per-sample sequences are stored as tuples. Construction validates these
    structural invariants and raises ``ValueError`` / ``TypeError`` on violation.
    """

    start_time: datetime
    end_time: datetime
    resolution_s: int
    timestamps: tuple[datetime, ...]
    values: Mapping[Metric, tuple[float | None, ...]] = field(hash=False)
    missing: Mapping[Metric, tuple[bool, ...]] = field(hash=False)
    stages: tuple[Sleep_Stage, ...]

    def __post_init__(self) -> None:
        expected = sample_timestamps(self.start_time, self.end_time, self.resolution_s)
        timestamps = tuple(self.timestamps)
        if len(timestamps) != len(expected):
            raise ValueError(
                f"expected {len(expected)} sample timestamps, got {len(timestamps)}"
            )
        for i, (actual, wanted) in enumerate(zip(timestamps, expected)):
            _require_aware(actual, f"timestamps[{i}]")
            if actual != wanted:
                raise ValueError(f"timestamps[{i}] is not start_time + {i} * resolution_s")
        object.__setattr__(self, "timestamps", timestamps)

        n = len(timestamps)
        values = {m: tuple(seq) for m, seq in dict(self.values).items()}
        missing = {m: tuple(seq) for m, seq in dict(self.missing).items()}
        if set(values) != set(missing):
            raise ValueError("values and missing must have the same metric keys")
        for metric in values:
            if not isinstance(metric, Metric):
                raise TypeError(f"metric keys must be Metric, got {metric!r}")
            vals, mask = values[metric], missing[metric]
            if len(vals) != n or len(mask) != n:
                raise ValueError(f"{metric.value}: values and missing need {n} samples")
            for i, (v, is_missing) in enumerate(zip(vals, mask)):
                if not isinstance(is_missing, bool):
                    raise TypeError(f"{metric.value}: missing[{i}] must be a bool")
                if is_missing:
                    if v is not None:
                        raise ValueError(f"{metric.value}: missing sample {i} must have value None")
                elif not _is_finite_number(v):
                    raise ValueError(f"{metric.value}: sample {i} must be a finite number")
        object.__setattr__(self, "values", values)
        object.__setattr__(self, "missing", missing)

        stages = tuple(self.stages)
        if len(stages) != n:
            raise ValueError(f"stages needs {n} entries, got {len(stages)}")
        if not all(isinstance(s, Sleep_Stage) for s in stages):
            raise TypeError("stages must contain only Sleep_Stage values")
        object.__setattr__(self, "stages", stages)

    @property
    def sample_count(self) -> int:
        """Number of samples."""
        return len(self.timestamps)

    @property
    def duration(self) -> timedelta:
        """Elapsed session duration (UTC instants)."""
        return self.end_time.astimezone(timezone.utc) - self.start_time.astimezone(timezone.utc)

    def sample_interval(self, index: int) -> tuple[datetime, datetime]:
        """Half-open Sample_Interval ``[start, end)`` of sample ``index``."""
        start = self.timestamps[index]
        if index < 0:
            index += len(self.timestamps)
        if index + 1 < len(self.timestamps):
            return start, self.timestamps[index + 1]
        return start, self.end_time.astimezone(timezone.utc)

    def is_missing(self, metric: Metric, index: int) -> bool:
        """Missing_Data_Mask lookup; metrics absent from the timeline count as missing."""
        mask = self.missing.get(metric)
        return True if mask is None else mask[index]
