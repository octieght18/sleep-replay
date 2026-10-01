"""Sanity tests for Fitbit HRV parsing (task 7.5). Full coverage lives in 7.9."""

from __future__ import annotations

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from backend.domain.adapter import SessionHrvRow
from backend.domain.errors import Import_Report
from backend.domain.session import SleepSession
from backend.domain.telemetry import Metric
from backend.ingestion.fitbit.hrv import parse_hrv_details, parse_hrv_summary, session_hrv_for

BERLIN = ZoneInfo("Europe/Berlin")


def test_details_rows_become_utc_hrv_points_with_skips() -> None:
    data = (
        "\ufeffTimestamp,RMSSD,coverage,low_frequency\n"
        "2024-01-02T01:00:00,42.5,0.9,100\n"
        "\n"
        "2024-01-02T01:05:00+00:00,30,0.9,100\n"
        "not-a-time,40,0.9,100\n"
        "2024-01-02T01:10:00,0,0.9,100\n"
        "2024-01-02T01:15:00,abc,0.9,100\n"
        "2024-01-02T01:20:00,500,0.9,100\n"
    ).encode("utf-8")
    report = Import_Report()
    result = parse_hrv_details(data, "hrv.csv", BERLIN, report=report)

    assert [p.value for p in result.points] == [42.5, 30.0, 500.0]
    first = result.points[0]
    assert first.metric is Metric.hrv_rmssd and first.unit == "ms" and first.source == "fitbit"
    assert first.timestamp == datetime(2024, 1, 2, 0, 0, tzinfo=timezone.utc)
    assert first.timestamp.utcoffset().total_seconds() == 0
    assert result.points[1].timestamp == datetime(2024, 1, 2, 1, 5, tzinfo=timezone.utc)
    assert (result.skipped_row_count, result.skipped_value_count) == (1, 2)
    assert (report.skipped_row_count, report.skipped_value_count) == (1, 2)


def test_missing_rmssd_column_skips_file() -> None:
    report = Import_Report()
    result = parse_hrv_details("timestamp,coverage\n2024-01-02T01:00:00,1\n", "hrv.csv", BERLIN, report=report)
    assert result.points == [] and result.skipped
    assert report.skipped_files and "'rmssd'" in report.skipped_files[0][1]


def test_summary_rows_and_session_hrv_mean() -> None:
    data = (
        "timestamp,rmssd,nremhr,entropy\n"
        "2024-01-02T00:00:00,40,55,2.1\n"
        "2024-01-02,50,55,2.1\n"
        "2024-01-03T00:00:00,600,55,2.1\n"
        "bad,40,1,1\n"
    )
    result = parse_hrv_summary(data, "summary.csv")
    assert result.rows == [
        SessionHrvRow(date(2024, 1, 2), 40.0, "summary.csv"),
        SessionHrvRow(date(2024, 1, 2), 50.0, "summary.csv"),
    ]
    assert (result.skipped_row_count, result.skipped_value_count) == (1, 1)

    # Ends 23:30 UTC on 1 Jan = 00:30 on 2 Jan in Berlin.
    session = SleepSession(
        datetime(2024, 1, 1, 16, 0, tzinfo=timezone.utc),
        datetime(2024, 1, 1, 23, 30, tzinfo=timezone.utc),
        "fitbit",
    )
    assert session_hrv_for(session, result.rows, BERLIN) == 45.0
    assert session_hrv_for(session, result.rows, ZoneInfo("UTC")) is None
