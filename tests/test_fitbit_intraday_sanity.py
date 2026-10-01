"""Sanity checks for Fitbit heart rate and steps parsing (full tests in task 7.9)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from backend.domain.errors import Import_Report
from backend.domain.telemetry import Metric, is_valid
from backend.domain.timezones import FITBIT_STEPS, DstAdjustmentCounter
from backend.ingestion.fitbit.intraday import parse_heart_rate, parse_intraday, parse_number, parse_steps

UTC = ZoneInfo("UTC")


def _dump(entries: object) -> bytes:
    return json.dumps(entries).encode("utf-8")


def test_heart_rate_points_and_skip_counts() -> None:
    entries = [
        {"dateTime": "01/02/24 03:04:05", "value": {"bpm": 62, "confidence": 3}},
        {"dateTime": "01/02/24 03:04:10", "value": {"bpm": "20"}},  # lower bound kept
        {"dateTime": "01/02/24 03:04:15", "value": {"bpm": 251}},  # out of range -> value
        {"dateTime": "01/02/24 03:04:20", "value": {"bpm": None}},  # empty -> value
        {"dateTime": "01/02/24 03:04:25", "value": {"bpm": "abc"}},  # non-numeric -> value
        {"dateTime": "2024-01-02 03:04:30", "value": {"bpm": 60}},  # bad dateTime -> row
        {"dateTime": "bad", "value": {"bpm": 999}},  # bad dateTime wins -> row
        {"dateTime": "01/02/24 03:04:35", "value": {}},  # missing value.bpm -> row
        {"value": {"bpm": 60}},  # missing dateTime -> row
        "not an object",  # -> row
    ]
    report = Import_Report()
    result = parse_heart_rate(_dump(entries), "heart_rate-2024-01-02.json", UTC, report=report)

    assert [p.value for p in result.points] == [62.0, 20.0]
    first = result.points[0]
    assert first.timestamp == datetime(2024, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    assert first.timestamp.tzinfo is timezone.utc
    assert (first.metric, first.unit, first.source) == (Metric.heart_rate, "bpm", "fitbit")
    assert all(is_valid(p) for p in result.points)
    assert (result.skipped_value_count, result.skipped_row_count) == (3, 5)
    assert (report.skipped_value_count, report.skipped_row_count) == (3, 5)
    assert report.skipped_files == []


def test_steps_string_and_numeric_values_with_range_gate() -> None:
    entries = [
        {"dateTime": "03/10/24 00:00:00", "value": "12"},
        {"dateTime": "03/10/24 00:01:00", "value": 0},
        {"dateTime": "03/10/24 00:02:00", "value": "300"},
        {"dateTime": "03/10/24 00:03:00", "value": "301"},
        {"dateTime": "03/10/24 00:04:00", "value": -1},
        {"dateTime": "03/10/24 00:05:00", "value": ""},
        {"dateTime": "03/10/24 00:06:00", "value": "NaN"},
    ]
    result = parse_intraday(FITBIT_STEPS, _dump(entries), "steps-2024-03-10.json", UTC)
    assert [p.value for p in result.points] == [12.0, 0.0, 300.0]
    assert all(p.metric is Metric.steps and p.unit == "steps/min" for p in result.points)
    assert (result.skipped_value_count, result.skipped_row_count) == (4, 0)


def test_non_utc_source_timezone_converts_to_utc_across_fall_back() -> None:
    tz = ZoneInfo("America/New_York")
    entries = [
        {"dateTime": "11/03/24 00:59:00", "value": 1},  # EDT
        {"dateTime": "11/03/24 01:30:00", "value": 2},  # ambiguous -> earlier (EDT)
        {"dateTime": "11/03/24 02:00:00", "value": 3},  # EST
    ]
    counter = DstAdjustmentCounter()
    result = parse_steps(_dump(entries), "steps-2024-11-03.json", tz, counter)
    stamps = [p.timestamp for p in result.points]
    assert stamps == [
        datetime(2024, 11, 3, 4, 59, tzinfo=timezone.utc),
        datetime(2024, 11, 3, 5, 30, tzinfo=timezone.utc),
        datetime(2024, 11, 3, 7, 0, tzinfo=timezone.utc),
    ]
    assert counter.count("steps-2024-11-03.json") == 1


def test_file_level_skips() -> None:
    report = Import_Report()
    bad = parse_heart_rate(b'[\n{"dateTime": ', "heart_rate-2024-01-02.json", UTC, report=report)
    assert bad.skipped and "line 2" in bad.file_skip_reason
    obj = parse_steps(b'{"a": 1}', "steps-2024-01-02.json", UTC, report=report)
    assert obj.skipped and obj.points == []
    assert [name for name, _ in report.skipped_files] == ["heart_rate-2024-01-02.json", "steps-2024-01-02.json"]


def test_parse_number() -> None:
    assert parse_number(" 7.5 ") == 7.5
    assert parse_number("1e2") == 100.0
    for raw in (None, "", "  ", True, "inf", "1_000", float("nan"), 10**400, [1]):
        assert parse_number(raw) is None
