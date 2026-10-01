"""Sanity tests for the Fitbit_Importer adapter (task 7.6).

Full packaging and parsing coverage lives in tasks 7.8 / 7.9.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from backend.domain.adapter import Data_Source_Adapter, SupportsCandidateSessions, TimezoneContext
from backend.domain.telemetry import Metric
from backend.domain.timezones import FITBIT_HEART_RATE, FITBIT_HRV_DETAILS, FITBIT_SLEEP, FITBIT_STEPS
from backend.ingestion.fitbit import (
    SOURCE_IDENTIFIER,
    FitbitImporter,
    add_missing_metric_warnings,
    files_in_selection_window,
    missing_metric_warnings,
)

NY = ZoneInfo("America/New_York")
UTC = timezone.utc

SLEEP_LOG = [
    {
        "logId": 1,
        "startTime": "2024-01-01T23:00:00.000",
        "endTime": "2024-01-02T06:00:00.000",
        "type": "stages",
        "isMainSleep": True,
        "levels": {
            "data": [
                {"dateTime": "2024-01-01T23:00:00.000", "level": "light", "seconds": 12600},
                {"dateTime": "2024-01-02T02:30:00.000", "level": "deep", "seconds": 12600},
            ]
        },
    }
]


@pytest.fixture()
def export_files(tmp_path):
    files = {
        "sleep-2024-01-01.json": json.dumps(SLEEP_LOG),
        # Heart rate and steps default to UTC; 05:00Z is inside the session (04:00Z-11:00Z).
        "heart_rate-2024-01-02.json": json.dumps(
            [{"dateTime": "01/02/24 05:00:00", "value": {"bpm": 58}},
             {"dateTime": "01/02/24 06:00:00", "value": {"bpm": 999}}]  # out of range
        ),
        "heart_rate-2024-01-10.json": json.dumps([{"dateTime": "01/10/24 05:00:00", "value": {"bpm": 60}}]),
        "steps-2024-01-02.json": json.dumps([{"dateTime": "01/02/24 05:01:00", "value": "3"}]),
        # HRV details default to the Display_Timezone: 01:00 New York = 06:00Z.
        "Heart Rate Variability Details - 2024-01-02.csv": "timestamp,rmssd,coverage\n2024-01-02T01:00:00,40.5,0.9\n",
        "Daily Heart Rate Variability Summary - 2024-01.csv": "timestamp,rmssd\n2024-01-02,35\n",
        "notes.txt": "not a fitbit file",
    }
    paths = []
    for name, text in files.items():
        p = tmp_path / name
        p.write_text(text, encoding="utf-8")
        paths.append(p)
    return paths


def _ctx() -> TimezoneContext:
    return TimezoneContext(NY)


def test_importer_satisfies_adapter_protocols():
    importer = FitbitImporter()
    assert importer.source_identifier == SOURCE_IDENTIFIER == "fitbit"
    assert isinstance(importer, Data_Source_Adapter)
    assert isinstance(importer, SupportsCandidateSessions)


def test_load_combines_files_and_fills_report(export_files):
    result = FitbitImporter().load(export_files, _ctx())
    report = result.report

    assert len(result.sessions) == 1 and result.sessions[0].log_id == "1"
    assert result.sessions[0].start_time == datetime(2024, 1, 2, 4, 0, tzinfo=UTC)
    assert set(report.accepted_files) == {p.name for p in export_files if p.suffix != ".txt"}
    assert report.unsupported_files == ["notes.txt"]
    assert report.skipped_value_count == 1  # bpm 999

    assert report.per_metric_counts == {"heart_rate": 2, "hrv_rmssd": 1, "steps": 1}
    assert report.per_metric_coverage["hrv_rmssd"] == (
        datetime(2024, 1, 2, 6, 0, tzinfo=UTC),
        datetime(2024, 1, 2, 6, 0, tzinfo=UTC),
    )
    assert report.per_metric_coverage["heart_rate"][1] == datetime(2024, 1, 10, 5, 0, tzinfo=UTC)

    assert report.applied_source_timezones[FITBIT_SLEEP] == "America/New_York"
    assert report.applied_source_timezones[FITBIT_HEART_RATE] == "UTC"
    assert report.applied_source_timezones[FITBIT_STEPS] == "UTC"
    assert report.applied_source_timezones[FITBIT_HRV_DETAILS] == "America/New_York"

    assert result.file_dates["heart_rate-2024-01-10.json"] == date(2024, 1, 10)
    assert result.file_dates["Daily Heart Rate Variability Summary - 2024-01.csv"] == date(2024, 1, 1)
    assert [(r.date, r.rmssd) for r in result.session_hrv] == [(date(2024, 1, 2), 35.0)]


def test_candidate_sessions_match_load_sessions(export_files):
    importer = FitbitImporter()
    loaded = importer.load(export_files, _ctx())
    assert importer.candidate_sessions(export_files, _ctx()) == loaded.sessions
    # A fresh importer (cache miss) reads only the sleep files and agrees.
    assert FitbitImporter().candidate_sessions(export_files, _ctx()) == loaded.sessions


def test_override_changes_applied_timezone(export_files):
    ctx = TimezoneContext(NY, {FITBIT_HEART_RATE: "Europe/Berlin"})
    report = FitbitImporter().load(export_files, ctx).report
    assert report.applied_source_timezones[FITBIT_HEART_RATE] == "Europe/Berlin"


def test_selection_window_limits_intraday_files(export_files):
    importer = FitbitImporter()
    session = importer.candidate_sessions(export_files, _ctx())[0]
    result = importer.load(export_files, _ctx(), selection=session)
    assert "heart_rate-2024-01-10.json" not in result.report.accepted_files
    assert "heart_rate-2024-01-02.json" in result.report.accepted_files
    assert result.report.per_metric_counts["heart_rate"] == 1
    # Every metric is present in the session, so no missing-metric warning.
    assert not [w for w in result.report.warnings if w.subject in {m.value for m in Metric}]


def test_files_in_selection_window_uses_display_dates():
    dates = {"a": date(2023, 12, 31), "b": date(2024, 1, 3), "c": date(2024, 1, 4), "d": None}
    start = datetime(2024, 1, 2, 4, 0, tzinfo=UTC)  # 2024-01-01 23:00 in New York
    end = datetime(2024, 1, 2, 11, 0, tzinfo=UTC)
    assert files_in_selection_window(dates, start, end, NY) == ["a", "b"]


def test_missing_metric_warnings_once_per_metric(export_files):
    importer = FitbitImporter()
    result = importer.load(export_files, _ctx())
    session = result.sessions[0]
    no_steps = [p for p in result.telemetry if p.metric is not Metric.steps]
    assert [w.subject for w in missing_metric_warnings(session, no_steps)] == ["steps"]

    # Without in-session HRV points, the daily summary row supplies Session_HRV.
    no_hrv = [p for p in result.telemetry if p.metric is not Metric.hrv_rmssd]
    assert missing_metric_warnings(session, no_hrv, result.session_hrv, NY) == []
    assert [w.subject for w in missing_metric_warnings(session, no_hrv)] == ["hrv_rmssd"]

    report = result.report
    first = add_missing_metric_warnings(report, session, [], (), NY)
    again = add_missing_metric_warnings(report, session, [], (), NY)
    assert [w.subject for w in first] == ["heart_rate", "hrv_rmssd", "steps"]
    assert again == []
