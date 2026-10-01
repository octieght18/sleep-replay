"""Unit tests for backend.domain.telemetry and backend.domain.units (task 2.7).

Requirements 1.3, 1.4, 1.8 (TelemetryPoint validity) and 10.3 (unit conversion).
"""

from __future__ import annotations

import dataclasses
import math
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from backend.domain import units
from backend.domain.telemetry import (
    CANONICAL_UNIT,
    CONTINUOUS_METRICS,
    Metric,
    TelemetryPoint,
    is_valid,
)
from backend.domain.units import (
    ALLOWED_UNITS,
    InconvertibleUnitError,
    is_convertible,
    to_canonical,
)

TS = datetime(2024, 3, 1, 23, 30, tzinfo=timezone(timedelta(hours=-5)))


def point(**overrides) -> TelemetryPoint:
    fields = dict(timestamp=TS, source="sensorpush:A1", metric=Metric.temperature, value=21.5, unit="°C")
    fields.update(overrides)
    return TelemetryPoint(**fields)


# --- Metric, Continuous_Metric, Canonical_Unit --------------------------------


def test_metric_names_and_canonical_units_match_glossary():
    assert [m.value for m in Metric] == [
        "heart_rate", "hrv_rmssd", "steps", "temperature", "humidity", "pressure",
    ]
    assert CANONICAL_UNIT == {
        Metric.heart_rate: "bpm",
        Metric.hrv_rmssd: "ms",
        Metric.steps: "steps/min",
        Metric.temperature: "°C",
        Metric.humidity: "percent",
        Metric.pressure: "hPa",
    }
    assert CONTINUOUS_METRICS == set(Metric) - {Metric.steps}


def test_allowed_units_match_requirement_1_4():
    assert {m: set(u) for m, u in ALLOWED_UNITS.items()} == {
        Metric.heart_rate: {"bpm"},
        Metric.hrv_rmssd: {"ms"},
        Metric.steps: {"steps/min"},
        Metric.temperature: {"°C", "°F"},
        Metric.humidity: {"percent"},
        Metric.pressure: {"hPa", "mbar", "inHg", "kPa"},
    }
    assert units.CELSIUS == "\u00b0C" and units.FAHRENHEIT == "\u00b0F"


# --- TelemetryPoint validity (Requirement 1.8) -----------------------------------


def test_telemetry_point_is_frozen():
    p = point()
    with pytest.raises(dataclasses.FrozenInstanceError):
        p.value = 1.0  # type: ignore[misc]


@pytest.mark.parametrize(
    "overrides",
    [
        {},
        {"value": 0},  # int values are numeric
        {"value": -40.0, "unit": "°F"},
        {"timestamp": datetime(2024, 3, 1, 23, 30, tzinfo=ZoneInfo("Europe/Berlin"))},
        {"timestamp": datetime(2024, 3, 1, 23, 30, tzinfo=timezone(timedelta(hours=5, seconds=7)))},  # whole seconds
        {"metric": Metric.pressure, "value": 29.92, "unit": "inHg"},
        {"metric": Metric.steps, "value": 12.0, "unit": "steps/min"},
    ],
)
def test_is_valid_accepts_valid_points(overrides):
    assert is_valid(point(**overrides)) is True


@pytest.mark.parametrize(
    "overrides",
    [
        {"timestamp": datetime(2024, 3, 1, 23, 30)},  # naive
        {"timestamp": "2024-03-01T23:30:00-05:00"},  # not a datetime
        # UTC offset not a whole number of seconds (Req 1.2)
        {"timestamp": datetime(2024, 3, 1, 23, 30, tzinfo=timezone(timedelta(microseconds=1)))},
        {"timestamp": datetime(2024, 3, 1, 23, 30, tzinfo=timezone(timedelta(hours=-5, microseconds=-1)))},
        {"source": ""},
        {"source": None},
        {"metric": "temperature"},  # plain string, not a Metric
        {"value": math.nan},
        {"value": math.inf},
        {"value": -math.inf},
        {"value": True},  # bool is not a measurement
        {"value": "21.5"},
        {"value": None},
        {"unit": "C"},  # header token, not a unit constant
        {"unit": "°c"},  # matching is case-sensitive
        {"unit": "hPa"},  # allowed unit of another metric
        {"unit": None},
    ],
)
def test_is_valid_rejects_invalid_points(overrides):
    assert is_valid(point(**overrides)) is False


# --- Unit conversion (Requirement 10.3) -------------------------------------------


@pytest.mark.parametrize(
    ("metric", "value", "unit", "expected"),
    [
        (Metric.temperature, 32.0, "°F", 0.0),
        (Metric.temperature, 212.0, "°F", 100.0),
        (Metric.temperature, -40.0, "°F", -40.0),
        (Metric.temperature, 68.0, "°F", 20.0),
        (Metric.temperature, 21.5, "°C", 21.5),
        (Metric.pressure, 1.0, "inHg", 33.86389),
        (Metric.pressure, 29.92, "inHg", 29.92 * 33.86389),
        (Metric.pressure, 101.325, "kPa", 1013.25),
        (Metric.pressure, 1013.25, "mbar", 1013.25),
        (Metric.pressure, 1013.25, "hPa", 1013.25),
        (Metric.heart_rate, 58.0, "bpm", 58.0),
        (Metric.hrv_rmssd, 42.5, "ms", 42.5),
        (Metric.steps, 12.0, "steps/min", 12.0),
        (Metric.humidity, 45.0, "percent", 45.0),
    ],
)
def test_to_canonical_values(metric, value, unit, expected):
    result = to_canonical(metric, value, unit)
    assert isinstance(result, float)
    assert result == pytest.approx(expected, rel=1e-12, abs=1e-12)


def test_to_canonical_accepts_int_values():
    assert to_canonical(Metric.temperature, 50, "°F") == pytest.approx(10.0)


@pytest.mark.parametrize(
    ("metric", "unit"),
    [
        (Metric.heart_rate, "°C"),
        (Metric.temperature, "hPa"),
        (Metric.temperature, "K"),
        (Metric.pressure, "psi"),
        (Metric.humidity, "%"),
        (Metric.pressure, None),
        ("pressure", "hPa"),  # metric must be a Metric
    ],
)
def test_to_canonical_rejects_inconvertible_units(metric, unit):
    assert is_convertible(metric, unit) is False
    with pytest.raises(InconvertibleUnitError) as exc:
        to_canonical(metric, 1.0, unit)
    assert exc.value.metric == metric and exc.value.unit == unit
    assert isinstance(exc.value, ValueError)


def test_inconvertible_unit_error_message_names_metric_and_unit():
    with pytest.raises(InconvertibleUnitError, match=r"'psi'.*pressure"):
        to_canonical(Metric.pressure, 1.0, "psi")


def test_every_allowed_unit_is_convertible():
    for metric, allowed in ALLOWED_UNITS.items():
        for unit in allowed:
            assert is_convertible(metric, unit)
            assert math.isfinite(to_canonical(metric, 1.0, unit))
