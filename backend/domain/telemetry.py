"""Normalized telemetry model (Requirement 1).

Defines the MVP ``Metric`` names, the ``Continuous_Metric`` set, each metric's
``Canonical_Unit``, the immutable ``TelemetryPoint`` value object, and the
validity predicate used by the adapter validation gate (Requirement 7.7) and by
persistence (Requirement 1.8, 1.10).

A TelemetryPoint stores its value in the *source* unit exactly as parsed
(Requirement 1.4). Conversion to the Canonical_Unit happens only in the Aligner,
through :func:`backend.domain.units.to_canonical` (Requirement 10.3).

This module imports only the standard library and other ``backend.domain``
modules (Dependency_Rules, Requirement 17.2).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class Metric(str, Enum):
    """The six MVP metrics (Requirement 1.3). Values are the stable wire names."""

    heart_rate = "heart_rate"
    hrv_rmssd = "hrv_rmssd"
    steps = "steps"
    temperature = "temperature"
    humidity = "humidity"
    pressure = "pressure"


#: Metrics that may be interpolated across short gaps (glossary: Continuous_Metric).
#: ``steps`` is deliberately excluded.
CONTINUOUS_METRICS: frozenset[Metric] = frozenset(
    {
        Metric.heart_rate,
        Metric.hrv_rmssd,
        Metric.temperature,
        Metric.humidity,
        Metric.pressure,
    }
)

#: Canonical_Unit per metric (glossary). The strings are identical to the unit
#: constants in :mod:`backend.domain.units` (``BPM``, ``MS``, ``STEPS_PER_MIN``,
#: ``CELSIUS``, ``PERCENT``, ``HPA``); ``units`` asserts this at import time.
#: They are literals here so this module does not import ``units`` at load time
#: (``units`` depends on ``Metric``).
CANONICAL_UNIT: dict[Metric, str] = {
    Metric.heart_rate: "bpm",
    Metric.hrv_rmssd: "ms",
    Metric.steps: "steps/min",
    Metric.temperature: "°C",
    Metric.humidity: "percent",
    Metric.pressure: "hPa",
}


@dataclass(frozen=True)
class TelemetryPoint:
    """One normalized measurement ``(timestamp, source, metric, value, unit)``.

    - ``timestamp``: timezone-aware, carrying an explicit UTC offset that is a
      whole number of seconds (Req 1.2).
    - ``source``: the Source_Identifier, optionally plus a sensor id (Req 1.1).
    - ``metric``: exactly one MVP :class:`Metric` (Req 1.3).
    - ``value``: the value as parsed, in the source unit; finite (Req 1.4, 1.5).
    - ``unit``: the source unit, one of ``units.ALLOWED_UNITS[metric]`` (Req 1.4).
    """

    timestamp: datetime
    source: str
    metric: Metric
    value: float
    unit: str


def is_valid(point: TelemetryPoint) -> bool:
    """Return True iff ``point`` is a valid TelemetryPoint (Requirement 1.8).

    Valid means: a timezone-aware timestamp whose UTC offset is a whole number
    of seconds (Req 1.2), a non-empty source string, a :class:`Metric`, a
    finite numeric value, and a unit listed for the metric in Requirement 1.4
    (``units.ALLOWED_UNITS``). Never raises for malformed fields.

    Offsets with a sub-second part (e.g. ``timezone(timedelta(microseconds=1))``)
    are rejected: no real source produces them and ISO 8601 text cannot carry
    them reliably (CPython reads ``+00:00:00.000001`` back as UTC), so they
    would break the persistence round trip (Req 1.8).
    """
    # Deferred import: ``units`` imports ``Metric`` from this module.
    from backend.domain.units import ALLOWED_UNITS

    ts = point.timestamp
    if not isinstance(ts, datetime) or ts.tzinfo is None:
        return False
    offset = ts.utcoffset()
    if offset is None or offset.microseconds != 0:
        return False
    if not isinstance(point.source, str) or point.source == "":
        return False
    if not isinstance(point.metric, Metric):
        return False
    value = point.value
    # bool is an int subclass but is not a numeric measurement.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    if not math.isfinite(value):
        return False
    if not isinstance(point.unit, str):
        return False
    return point.unit in ALLOWED_UNITS[point.metric]
