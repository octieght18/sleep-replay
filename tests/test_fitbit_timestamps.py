"""Example tests for Fitbit timestamp parsing (Requirements 4.1, 4.2, 4.4, 9.1, 9.3, 9.4)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from backend.domain.timezones import DstAdjustmentCounter
from backend.ingestion.fitbit.timestamps import (
    TimestampParseError,
    localize_iso,
    localize_mdy,
    parse_iso_datetime,
    parse_mdy_datetime,
)

UTC = ZoneInfo("UTC")
NEW_YORK = ZoneInfo("America/New_York")


# --- MM/DD/YY HH:MM:SS -------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("01/02/24 23:05:07", datetime(2024, 1, 2, 23, 5, 7)),
        ("12/31/99 00:00:00", datetime(2099, 12, 31, 0, 0, 0)),
        ("01/01/00 00:00:00", datetime(2000, 1, 1, 0, 0, 0)),
        ("  02/29/24 12:00:00 ", datetime(2024, 2, 29, 12, 0, 0)),
    ],
)
def test_parse_mdy_reads_two_digit_years_as_2000s(text: str, expected: datetime) -> None:
    assert parse_mdy_datetime(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "13/01/24 00:00:00",
        "02/30/24 00:00:00",
        "01/01/24 24:00:00",
        "01/01/2024 00:00:00",
        "1/1/24 00:00:00",
        "01/01/24",
        "2024-01-01T00:00:00",
        None,
        20240101,
    ],
)
def test_parse_mdy_rejects_other_input(text: object) -> None:
    with pytest.raises(TimestampParseError):
        parse_mdy_datetime(text)


def test_timestamp_parse_error_is_a_value_error() -> None:
    with pytest.raises(ValueError):
        parse_mdy_datetime("garbage")


def test_localize_mdy_uses_source_timezone() -> None:
    result = localize_mdy("01/02/24 23:05:07", UTC)
    assert result == datetime(2024, 1, 2, 23, 5, 7, tzinfo=timezone.utc)
    assert result.utcoffset() == timedelta(0)


# --- ISO 8601 ----------------------------------------------------------------


def test_parse_iso_sleep_log_format_is_naive_with_milliseconds() -> None:
    assert parse_iso_datetime("2024-01-01T23:05:30.500") == datetime(2024, 1, 1, 23, 5, 30, 500000)


def test_parse_iso_keeps_explicit_offsets() -> None:
    assert parse_iso_datetime("2024-01-01T23:05:00Z").utcoffset() == timedelta(0)
    assert parse_iso_datetime("2024-01-01T23:05:00+05:30").utcoffset() == timedelta(hours=5, minutes=30)
    assert parse_iso_datetime("2024-01-01T23:05:00-08:00").utcoffset() == timedelta(hours=-8)


@pytest.mark.parametrize("text", ["", "   ", "not a date", "2024-13-01T00:00:00", "01/01/24 00:00:00", None])
def test_parse_iso_rejects_other_input(text: object) -> None:
    with pytest.raises(TimestampParseError):
        parse_iso_datetime(text)


def test_localize_iso_offset_free_uses_source_timezone() -> None:
    result = localize_iso("2024-01-01T23:05:00", NEW_YORK)
    assert result.astimezone(timezone.utc) == datetime(2024, 1, 2, 4, 5, tzinfo=timezone.utc)


def test_localize_iso_explicit_offset_ignores_source_timezone() -> None:
    result = localize_iso("2024-01-01T23:05:00+01:00", NEW_YORK)
    assert result.astimezone(timezone.utc) == datetime(2024, 1, 1, 22, 5, tzinfo=timezone.utc)
    assert result.utcoffset() == timedelta(hours=1)


# --- DST handling and counting -------------------------------------------------


def test_dst_adjustments_are_counted_per_file() -> None:
    counter = DstAdjustmentCounter()
    # Spring forward 2024-03-10 02:00 -> 03:00 in New York: 02:30 is nonexistent.
    skipped = localize_iso("2024-03-10T02:30:00", NEW_YORK, "sleep-2024-03-10.json", counter)
    assert skipped.replace(tzinfo=None) == datetime(2024, 3, 10, 3, 30)
    # Fall back 2024-11-03 01:00-02:00 repeats: earlier instant (EDT, UTC-4).
    repeated = localize_mdy("11/03/24 01:30:00", NEW_YORK, "heart_rate-2024-11-03.json", counter)
    assert repeated.astimezone(timezone.utc) == datetime(2024, 11, 3, 5, 30, tzinfo=timezone.utc)
    # Unaffected, explicit-offset, and unparseable timestamps are not counted.
    localize_iso("2024-03-10T04:00:00", NEW_YORK, "sleep-2024-03-10.json", counter)
    localize_iso("2024-03-10T02:30:00-05:00", NEW_YORK, "sleep-2024-03-10.json", counter)
    with pytest.raises(TimestampParseError):
        localize_iso("bad", NEW_YORK, "sleep-2024-03-10.json", counter)

    assert counter.counts == {"sleep-2024-03-10.json": 1, "heart_rate-2024-11-03.json": 1}
    assert [w.subject for w in counter.warnings()] == [
        "heart_rate-2024-11-03.json",
        "sleep-2024-03-10.json",
    ]


def test_localize_without_counter_still_adjusts() -> None:
    result = localize_iso("2024-03-10T02:30:00", NEW_YORK)
    assert result.replace(tzinfo=None) == datetime(2024, 3, 10, 3, 30)
