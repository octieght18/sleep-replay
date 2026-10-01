"""Property test for the TelemetryPoint persistence round trip (task 3.3).

Feature: sleep-replay-data-pipeline, Property 1: TelemetryPoint persistence round trip

*For any* list of valid TelemetryPoints (including the empty list), persisting
the list to the Data_Directory and then loading it returns a list of the same
length and order in which each TelemetryPoint has the same timestamp instant,
the same UTC offset, the same source, the same metric, the same unit, and an
exactly equal value.

**Validates: Requirements 1.8**

A valid TelemetryPoint's UTC offset is a whole number of seconds, so the
generators draw only whole-second offsets; sub-second offsets are covered by
the example tests at the end, which check that they are rejected as invalid.

Instants are compared with ``astimezone(timezone.utc)`` and offsets with
``utcoffset()``, never with datetime ``==``: under PEP 495 an aware datetime in
a repeated DST hour never compares equal to one with a different tzinfo, even
at the same instant.
"""

from __future__ import annotations

import math
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st

from backend.domain.telemetry import Metric, TelemetryPoint, is_valid
from backend.domain.units import ALLOWED_UNITS
from backend.persistence.telemetry_store import (
    TelemetryStore,
    deserialize_points,
    serialize_points,
)

IMPORT_ID = "import-roundtrip_01"

# Zones with DST, half-hour / 45-minute offsets, negative DST, skipped days,
# and LMT offsets with seconds in their early history.
_NAMED_ZONES = [
    "UTC",
    "America/New_York",
    "America/St_Johns",
    "Europe/London",
    "Europe/Dublin",
    "Europe/Amsterdam",
    "Australia/Lord_Howe",
    "Pacific/Chatham",
    "Pacific/Apia",
    "Pacific/Kiritimati",
    "Asia/Kolkata",
    "Asia/Kathmandu",
    "Africa/Casablanca",
]

_MAX_OFFSET_S = 24 * 3600 - 1  # timezone() requires |offset| < 24h

# Valid TelemetryPoints have whole-second UTC offsets (Requirement 1.2, 1.8),
# so fixed offsets are drawn in whole seconds only.
fixed_offsets = st.one_of(
    st.just(timezone.utc),
    # Whole-minute offsets, the common real-world case.
    st.integers(-23 * 60 - 59, 23 * 60 + 59).map(lambda m: timezone(timedelta(minutes=m))),
    # Arbitrary whole-second offsets, including sub-minute ones (e.g. +05:00:07, -00:00:01).
    st.integers(-_MAX_OFFSET_S, _MAX_OFFSET_S).map(lambda s: timezone(timedelta(seconds=s))),
)

zone_infos = st.one_of(
    st.sampled_from(_NAMED_ZONES).map(ZoneInfo),
    st.timezones(),
)

# Range kept well inside datetime's limits so astimezone(utc) cannot overflow
# for any offset; it still reaches LMT-era offsets with seconds.
general_timestamps = st.datetimes(
    min_value=datetime(1850, 1, 1),
    max_value=datetime(2100, 12, 31, 23, 59, 59, 999999),
    timezones=st.one_of(fixed_offsets, zone_infos),
)

# Wall-clock times inside a repeated (fall-back) DST hour, with either fold.
_REPEATED_HOURS = [
    ("America/New_York", datetime(2023, 11, 5, 1)),
    ("Europe/London", datetime(2023, 10, 29, 1)),
    ("Australia/Lord_Howe", datetime(2023, 4, 2, 1, 30)),  # 30-minute DST shift
    ("Europe/Dublin", datetime(2023, 10, 29, 1)),
]
_REPEATED_SPAN = {"Australia/Lord_Howe": timedelta(minutes=30)}


@st.composite
def repeated_hour_timestamps(draw: st.DrawFn) -> datetime:
    zone_name, start = draw(st.sampled_from(_REPEATED_HOURS))
    span = _REPEATED_SPAN.get(zone_name, timedelta(hours=1))
    delta_us = draw(st.integers(0, span // timedelta(microseconds=1) - 1))
    wall = start + timedelta(microseconds=delta_us)
    return wall.replace(tzinfo=ZoneInfo(zone_name), fold=draw(st.integers(0, 1)))


timestamps = st.one_of(general_timestamps, repeated_hour_timestamps())

# Any non-empty string, including lone surrogates and other non-ASCII text.
sources = st.one_of(
    st.sampled_from(["fitbit", "sensorpush", "sensorpush:HT1-0042"]),
    st.text(alphabet=st.characters(exclude_categories=()), min_size=1),
)

metric_units = st.sampled_from(
    [(metric, unit) for metric in Metric for unit in sorted(ALLOWED_UNITS[metric])]
)

# Finite values. Ints stay within float range: is_valid uses math.isfinite,
# which needs the int to be convertible to float.
values = st.one_of(
    st.integers(),
    st.integers(-(2**1000), 2**1000),
    st.floats(allow_nan=False, allow_infinity=False),
    st.sampled_from([0.0, -0.0, 5e-324, -5e-324, 1.7976931348623157e308, 0.1]),
)


@st.composite
def telemetry_points(draw: st.DrawFn) -> TelemetryPoint:
    metric, unit = draw(metric_units)
    point = TelemetryPoint(
        timestamp=draw(timestamps),
        source=draw(sources),
        metric=metric,
        value=draw(values),
        unit=unit,
    )
    assert is_valid(point)  # the generator only produces valid points
    return point


point_lists = st.lists(telemetry_points(), max_size=30)


def _assert_same_points(loaded: list[TelemetryPoint], original: list[TelemetryPoint]) -> None:
    assert len(loaded) == len(original)
    for index, (got, want) in enumerate(zip(loaded, original)):
        where = f"point {index}"
        assert got.timestamp.tzinfo is not None, where
        assert got.timestamp.astimezone(timezone.utc) == want.timestamp.astimezone(timezone.utc), where
        assert got.timestamp.utcoffset() == want.timestamp.utcoffset(), where
        assert got.source == want.source, where
        assert got.metric is want.metric, where
        assert got.unit == want.unit, where
        # Exactly equal value: same numeric type, same value, same sign of zero.
        assert type(got.value) is type(want.value), where
        assert got.value == want.value, where
        if isinstance(want.value, float):
            assert math.copysign(1.0, got.value) == math.copysign(1.0, want.value), where


# Feature: sleep-replay-data-pipeline, Property 1: TelemetryPoint persistence round trip
@settings(max_examples=200, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(points=point_lists)
@example(points=[])
def test_store_save_load_round_trip(points: list[TelemetryPoint], tmp_data_dir: Path) -> None:
    """Property 1 via TelemetryStore.save / .load on disk. **Validates: Requirements 1.8**"""
    # tmp_data_dir is shared across Hypothesis examples, so each example gets
    # its own fresh Data_Directory beneath it.
    with tempfile.TemporaryDirectory(dir=tmp_data_dir) as example_dir:
        store = TelemetryStore(example_dir)
        store.save(IMPORT_ID, points, source_files=["export.zip"])
        loaded = store.load(IMPORT_ID, source_files=["export.zip"])
        _assert_same_points(loaded, points)


# Feature: sleep-replay-data-pipeline, Property 1: TelemetryPoint persistence round trip
@settings(max_examples=200)
@given(points=point_lists)
@example(points=[])
def test_serialize_deserialize_round_trip(points: list[TelemetryPoint]) -> None:
    """Property 1 via serialize_points / deserialize_points. **Validates: Requirements 1.8**"""
    data = serialize_points(IMPORT_ID, points)
    _assert_same_points(deserialize_points(data, expected_import_id=IMPORT_ID), points)


# --- Example tests: sub-second offsets are invalid; whole-second ones round-trip ----


def _point_at(tz: timezone) -> TelemetryPoint:
    return TelemetryPoint(
        timestamp=datetime(2000, 1, 1, tzinfo=tz),
        source="fitbit",
        metric=Metric.heart_rate,
        value=60,
        unit="bpm",
    )


@pytest.mark.parametrize(
    "offset",
    [timedelta(microseconds=1), timedelta(microseconds=-1), timedelta(hours=5, seconds=7, microseconds=500)],
)
def test_sub_second_offset_is_invalid_and_not_serialized(offset: timedelta) -> None:
    point = _point_at(timezone(offset))
    assert is_valid(point) is False
    with pytest.raises(ValueError):
        serialize_points(IMPORT_ID, [point])


@pytest.mark.parametrize("offset", [timedelta(hours=5, seconds=7), timedelta(seconds=-1)])
def test_sub_minute_whole_second_offset_round_trips(offset: timedelta) -> None:
    point = _point_at(timezone(offset))
    assert is_valid(point) is True
    loaded = deserialize_points(serialize_points(IMPORT_ID, [point]), expected_import_id=IMPORT_ID)
    _assert_same_points(loaded, [point])
