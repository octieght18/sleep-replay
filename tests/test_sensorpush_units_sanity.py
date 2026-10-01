"""Sanity checks for SensorPush unit tokens and inference (Requirements 6.3, 6.4, 6.14).

Full coverage lands with task 8.5.
"""

from __future__ import annotations

import pytest

from backend.domain import units as du
from backend.domain.telemetry import Metric
from backend.ingestion.sensorpush.units import infer_unit, resolve_unit, unit_from_header


@pytest.mark.parametrize(
    ("metric", "header", "expected"),
    [
        (Metric.temperature, "Temperature (°F)", du.FAHRENHEIT),
        (Metric.temperature, "Temperature(F)", du.FAHRENHEIT),
        (Metric.temperature, "Temp Fahrenheit", du.FAHRENHEIT),
        (Metric.temperature, "Temperature (°C)", du.CELSIUS),
        (Metric.temperature, "temp_c", du.CELSIUS),
        (Metric.temperature, "Temperature Celsius", du.CELSIUS),
        (Metric.temperature, "Temperature (ºF)", du.FAHRENHEIT),
        (Metric.temperature, "Temperature", None),  # 'c'/'F' inside words don't count
        (Metric.temperature, "Temp Farm", None),
        (Metric.humidity, "Relative Humidity (%)", du.PERCENT),
        (Metric.humidity, "RH", None),
        (Metric.pressure, "Barometric Pressure (inHg)", du.INHG),
        (Metric.pressure, "Pressure mbar", du.MBAR),
        (Metric.pressure, "Pressure (hPa)", du.HPA),
        (Metric.pressure, "Pressure_kPa", du.KPA),
        (Metric.pressure, "Barometric Pressure", None),
    ],
)
def test_unit_from_header(metric: Metric, header: str, expected: str | None) -> None:
    assert unit_from_header(metric, header) == expected


def test_infer_unit_ranges() -> None:
    assert infer_unit(Metric.temperature, [68.0, 70.0, 71.0]) == du.FAHRENHEIT
    assert infer_unit(Metric.temperature, [45.0]) == du.CELSIUS
    assert infer_unit(Metric.humidity, []) == du.PERCENT
    assert infer_unit(Metric.pressure, [29.9]) == du.INHG
    assert infer_unit(Metric.pressure, [101.3]) == du.KPA
    assert infer_unit(Metric.pressure, [1013.0]) == du.HPA
    assert infer_unit(Metric.pressure, [50.0]) is None


def test_resolve_unit_header_wins_without_warning() -> None:
    r = resolve_unit(Metric.temperature, "Temperature (°C)", [80.0])
    assert r.unit == du.CELSIUS and not r.inferred and r.warning is None


def test_resolve_unit_inferred_warns_with_metric_and_unit() -> None:
    r = resolve_unit(Metric.temperature, "Temperature", [70.0, 71.0])
    assert r.unit == du.FAHRENHEIT and r.inferred
    assert r.warning is not None and r.warning.subject == "temperature"
    assert "temperature" in r.warning.description and du.FAHRENHEIT in r.warning.description


def test_resolve_unit_humidity_without_percent_warns() -> None:
    r = resolve_unit(Metric.humidity, "Humidity", [45.0])
    assert r.unit == du.PERCENT and r.warning is not None


def test_resolve_unit_undeterminable_pressure_drops_column() -> None:
    r = resolve_unit(Metric.pressure, "Pressure", [500.0], file_name="export.csv")
    assert r.unit is None and r.warning is not None
    assert "could not be determined" in r.warning.description
