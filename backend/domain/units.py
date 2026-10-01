"""Source units and canonical-unit conversion (Requirements 1.4, 7.1, 10.3).

Importers record each TelemetryPoint in its *source* unit, using exactly one of
the unit-string constants below (Requirement 1.4). The Aligner converts values
to the metric's Canonical_Unit with :func:`to_canonical` before deduplication,
sensor selection, and resampling (Requirement 10.3).

Unit-string constants (stable; persisted with every TelemetryPoint, so they
must not change):

========================  ===============  =============================
Constant                  String           Metric(s)
========================  ===============  =============================
``BPM``                   ``"bpm"``        heart_rate (canonical)
``MS``                    ``"ms"``         hrv_rmssd (canonical)
``STEPS_PER_MIN``         ``"steps/min"``  steps (canonical)
``CELSIUS``               ``"°C"``         temperature (canonical)
``FAHRENHEIT``            ``"°F"``         temperature
``PERCENT``               ``"percent"``    humidity (canonical)
``HPA``                   ``"hPa"``        pressure (canonical)
``MBAR``                  ``"mbar"``       pressure
``INHG``                  ``"inHg"``       pressure
``KPA``                   ``"kPa"``        pressure
========================  ===============  =============================

Matching is exact and case-sensitive. Header-token recognition (``F``,
``Fahrenheit``, ``%``, ...) is the importers' job; they map tokens to these
constants.

Stdlib only; imports no other Backend package (Requirement 17.2).
"""

from __future__ import annotations

from typing import Callable

from backend.domain.telemetry import CANONICAL_UNIT, Metric

# --- Unit-string constants ------------------------------------------------------

BPM = "bpm"
MS = "ms"
STEPS_PER_MIN = "steps/min"
CELSIUS = "°C"  # U+00B0 DEGREE SIGN + "C"
FAHRENHEIT = "°F"  # U+00B0 DEGREE SIGN + "F"
PERCENT = "percent"
HPA = "hPa"
MBAR = "mbar"
INHG = "inHg"
KPA = "kPa"

#: Allowed source units per metric (Requirement 1.4). Every unit here is
#: convertible to the metric's Canonical_Unit by :func:`to_canonical`.
ALLOWED_UNITS: dict[Metric, frozenset[str]] = {
    Metric.heart_rate: frozenset({BPM}),
    Metric.hrv_rmssd: frozenset({MS}),
    Metric.steps: frozenset({STEPS_PER_MIN}),
    Metric.temperature: frozenset({CELSIUS, FAHRENHEIT}),
    Metric.humidity: frozenset({PERCENT}),
    Metric.pressure: frozenset({HPA, MBAR, INHG, KPA}),
}

#: hPa per inHg (conventional inch of mercury at 0 °C: 3386.389 Pa).
HPA_PER_INHG = 33.86389
#: hPa per kPa.
HPA_PER_KPA = 10.0


class InconvertibleUnitError(ValueError):
    """Raised when a unit cannot be converted to a metric's Canonical_Unit.

    A plain exception (not a User_Error): the adapter validation gate and the
    Aligner catch it and turn it into Import_Report / Processing_Report counts.
    """

    def __init__(self, metric: object, unit: object) -> None:
        self.metric = metric
        self.unit = unit
        metric_name = metric.value if isinstance(metric, Metric) else repr(metric)
        super().__init__(
            f"unit {unit!r} cannot be converted to the canonical unit of metric {metric_name}"
        )


def _identity(value: float) -> float:
    return float(value)


def _fahrenheit_to_celsius(value: float) -> float:
    return (float(value) - 32.0) * 5.0 / 9.0


def _inhg_to_hpa(value: float) -> float:
    return float(value) * HPA_PER_INHG


def _kpa_to_hpa(value: float) -> float:
    return float(value) * HPA_PER_KPA


_CONVERTERS: dict[tuple[Metric, str], Callable[[float], float]] = {
    (Metric.heart_rate, BPM): _identity,
    (Metric.hrv_rmssd, MS): _identity,
    (Metric.steps, STEPS_PER_MIN): _identity,
    (Metric.temperature, CELSIUS): _identity,
    (Metric.temperature, FAHRENHEIT): _fahrenheit_to_celsius,
    (Metric.humidity, PERCENT): _identity,
    (Metric.pressure, HPA): _identity,
    (Metric.pressure, MBAR): _identity,  # 1 mbar == 1 hPa
    (Metric.pressure, INHG): _inhg_to_hpa,
    (Metric.pressure, KPA): _kpa_to_hpa,
}


def is_convertible(metric: object, unit: object) -> bool:
    """Return True iff ``unit`` is an allowed source unit for ``metric``."""
    return isinstance(metric, Metric) and isinstance(unit, str) and (metric, unit) in _CONVERTERS


def to_canonical(metric: Metric, value: float, unit: str) -> float:
    """Convert ``value`` from ``unit`` to the Canonical_Unit of ``metric``.

    °F → °C; inHg, kPa, and mbar → hPa; canonical units pass through unchanged
    (Requirement 10.3). Raises :class:`InconvertibleUnitError` if ``metric`` is
    not a :class:`Metric` or ``unit`` is not in ``ALLOWED_UNITS[metric]``.
    """
    if not is_convertible(metric, unit):
        raise InconvertibleUnitError(metric, unit)
    return _CONVERTERS[(metric, unit)](value)


# Keep telemetry.CANONICAL_UNIT, ALLOWED_UNITS, and the converter table in sync.
assert set(ALLOWED_UNITS) == set(Metric) == set(CANONICAL_UNIT)
assert all(CANONICAL_UNIT[m] in ALLOWED_UNITS[m] for m in Metric)
assert set(_CONVERTERS) == {(m, u) for m, units in ALLOWED_UNITS.items() for u in units}
