"""Unit tests for SensorPush timestamp parsing (Requirements 6.6, 6.8, 9.3, 9.4)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from backend.domain.timezones import DstAdjustmentCounter
from backend.ingestion.sensorpush.timestamps import (
    SUPPORTED_FORMATS,
    InvalidTimestamp,
    parse_and_localize,
    parse_timestamp,
)

BERLIN = ZoneInfo("Europe/Berlin")
NEW_YORK = ZoneInfo("America/New_York")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # ISO 8601, space or T, with or without seconds
        ("2024-03-01 22:05", datetime(2024, 3, 1, 22, 5)),
        ("2024-03-01 22:05:07", datetime(2024, 3, 1, 22, 5, 7)),
        ("2024-03-01T22:05", datetime(2024, 3, 1, 22, 5)),
        ("2024-03-01T22:05:07", datetime(2024, 3, 1, 22, 5, 7)),
        ("2024-03-01t22:05:07.25", datetime(2024, 3, 1, 22, 5, 7, 250000)),
        # US 12-hour, month/day/year
        ("3/1/2024 10:05 PM", datetime(2024, 3, 1, 22, 5)),
        ("03/01/2024 10:05:07 pm", datetime(2024, 3, 1, 22, 5, 7)),
        ("3/1/2024 12:00 AM", datetime(2024, 3, 1, 0, 0)),
        ("3/1/2024 12:30:00 PM", datetime(2024, 3, 1, 12, 30)),
        ("3/1/2024 1:00AM", datetime(2024, 3, 1, 1, 0)),
        # US 24-hour
        ("3/1/2024 22:05", datetime(2024, 3, 1, 22, 5)),
        ("12/31/2024 00:00:59", datetime(2024, 12, 31, 0, 0, 59)),
        ("1/12/2024 7:05", datetime(2024, 1, 12, 7, 5)),  # January 12, not 1 December
        # surrounding whitespace is tolerated
        ("  2024-03-01 22:05 \t", datetime(2024, 3, 1, 22, 5)),
    ],
)
def test_offset_free_formats_parse_to_naive_wall_time(text: str, expected: datetime) -> None:
    result = parse_timestamp(text)
    assert result == expected
    assert result.tzinfo is None


@pytest.mark.parametrize(
    ("text", "offset"),
    [
        ("2024-03-01T22:05:07Z", timedelta(0)),
        ("2024-03-01T22:05z", timedelta(0)),
        ("2024-03-01T22:05:07+01:00", timedelta(hours=1)),
        ("2024-03-01T22:05-05:30", -timedelta(hours=5, minutes=30)),
        ("2024-03-01 22:05:07 +02:00", timedelta(hours=2)),
    ],
)
def test_iso_with_explicit_offset_is_aware(text: str, offset: timedelta) -> None:
    result = parse_timestamp(text)
    assert result.utcoffset() == offset
    assert result.replace(tzinfo=None) == datetime(2024, 3, 1, 22, 5, result.second)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "not a date",
        "13/1/2024 10:00",  # month 13: slash dates are never day-first
        "31/12/2024 10:00 PM",
        "2/30/2024 10:00",
        "2024-02-30 10:00",
        "2024-03-01",  # date only
        "2024-03-01 25:00",
        "3/1/2024 13:00 PM",  # 12-hour clock hour out of range
        "3/1/2024 0:30 AM",
        "3/1/24 10:00",  # two-digit year
        "2024-03-01T22:05+0100",  # offset without colon
        "2024-03-01T22:05+24:00",
        "2024-03-01T22:05+01:60",
        "3/1/2024 10:00 +01:00",  # offsets only in ISO form
        "1709330700",
    ],
)
def test_unsupported_values_raise_invalid_timestamp(text: str) -> None:
    with pytest.raises(InvalidTimestamp):
        parse_timestamp(text)


def test_non_string_raises_invalid_timestamp_which_is_a_value_error() -> None:
    with pytest.raises(ValueError):
        parse_timestamp(None)


def test_explicit_offset_wins_over_source_timezone() -> None:
    result = parse_and_localize("2024-07-01T22:00:00+05:00", BERLIN)
    assert result.adjustment is None
    assert result.value.utcoffset() == timedelta(hours=5)
    assert result.value.astimezone(timezone.utc) == datetime(2024, 7, 1, 17, tzinfo=timezone.utc)


def test_offset_free_uses_source_timezone() -> None:
    summer = parse_and_localize("7/1/2024 10:00 PM", BERLIN).value
    winter = parse_and_localize("2024-01-15 22:00", BERLIN).value
    assert summer.utcoffset() == timedelta(hours=2)
    assert winter.utcoffset() == timedelta(hours=1)


def test_dst_ambiguous_and_nonexistent_times_are_adjusted_and_counted() -> None:
    counter = DstAdjustmentCounter()
    # Fall back in New York on 2024-11-03: 01:30 happens twice -> earlier (EDT).
    ambiguous = parse_and_localize("11/3/2024 1:30 AM", NEW_YORK)
    assert ambiguous.adjustment == "ambiguous"
    assert ambiguous.value.utcoffset() == timedelta(hours=-4)
    # Spring forward in Berlin on 2024-03-31: 02:30 doesn't exist -> 03:30 CEST.
    skipped = parse_and_localize("2024-03-31 02:30", BERLIN)
    assert skipped.adjustment == "nonexistent"
    assert skipped.value.replace(tzinfo=None) == datetime(2024, 3, 31, 3, 30)
    assert skipped.value.utcoffset() == timedelta(hours=2)

    counter.record("room.csv", ambiguous)
    counter.record("room.csv", skipped)
    counter.record("room.csv", parse_and_localize("2024-03-31 04:00", BERLIN))
    assert counter.count("room.csv") == 2
    assert len(counter.warnings()) == 1


def test_supported_formats_are_listed_for_error_messages() -> None:
    assert len(SUPPORTED_FORMATS) == 4
    assert any("AM/PM" in f for f in SUPPORTED_FORMATS)
