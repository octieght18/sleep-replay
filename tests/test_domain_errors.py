"""Unit tests for backend.domain.errors (Requirements 16.1, 16.4)."""

from __future__ import annotations

import json
import pickle
from datetime import datetime, timedelta, timezone

import pytest

from backend.domain import errors
from backend.domain.errors import (
    ERROR_CODE_TABLE,
    ERROR_CODES,
    NO_ACTION_REQUIRED,
    ErrorCode,
    Import_Report,
    Processing_Report,
    User_Error,
    Warning_Item,
    merge_import_reports,
    merge_processing_reports,
)

EXPECTED_CODES = {
    "NO_FITBIT_FILES",
    "MULTIPLE_ARCHIVES",
    "ARCHIVE_UNREADABLE",
    "NO_TIMESTAMP_COLUMN",
    "NO_READABLE_ROWS",
    "INVALID_TIMEZONE",
    "SESSION_TOO_SHORT",
    "INVALID_TARGET_DURATION",
    "NO_SLEEP_SESSION",
    "INVALID_MANUAL_RANGE",
    "SAVE_FAILED",
    "PERSISTED_DATA_UNREADABLE",
    "DATA_DIR_UNWRITABLE",
    "INSUFFICIENT_DATA",
    "SOURCE_LOAD_FAILED",
}

T0 = datetime(2024, 3, 1, 22, 0, tzinfo=timezone.utc)


def test_error_code_table_is_fixed_and_complete():
    assert ERROR_CODES == EXPECTED_CODES
    assert {c.value for c in ERROR_CODE_TABLE} == EXPECTED_CODES
    for code in EXPECTED_CODES:
        # String constants equal their enum values.
        assert getattr(errors, code) == code == ErrorCode(code)


def test_user_error_is_raisable_and_serializable():
    with pytest.raises(User_Error) as info:
        raise User_Error(
            errors.INVALID_TIMEZONE,
            "The timezone 'Mars/Base' is not a recognized timezone.",
            "Enter a timezone name such as America/New_York.",
            file_name="heart_rate_2024-03-01.json",
            details={"value": "Mars/Base"},
        )
    err = info.value
    assert err.code == "INVALID_TIMEZONE"
    assert err.error_code is ErrorCode.INVALID_TIMEZONE
    d = err.to_dict()
    assert json.loads(json.dumps(d)) == d
    assert User_Error.from_dict(d) == err
    assert pickle.loads(pickle.dumps(err)) == err
    # Positional (code, description, action) with enum code also works.
    assert User_Error(ErrorCode.SAVE_FAILED, "Could not save.", "Free disk space.").file_name is None


def test_user_error_rejects_unknown_code_and_empty_text():
    with pytest.raises(ValueError):
        User_Error("NOT_A_CODE", "desc", "act")
    with pytest.raises(ValueError):
        User_Error(errors.SAVE_FAILED, " ", "act")
    with pytest.raises(ValueError):
        User_Error(errors.SAVE_FAILED, "desc", "")


def test_user_error_fields_are_read_only():
    err = User_Error(errors.SAVE_FAILED, "Could not save.", "Free disk space.")
    with pytest.raises(AttributeError):
        err.code = errors.NO_SLEEP_SESSION  # type: ignore[misc]


def test_warning_item_defaults_and_round_trip():
    w = Warning_Item("Heart rate data is missing.", "heart_rate")
    assert w.recommended_action == NO_ACTION_REQUIRED
    assert Warning_Item.from_dict(w.to_dict()) == w


def _report(file: str, hr: int, start: datetime, end: datetime) -> Import_Report:
    r = Import_Report(accepted_files=[file], skipped_value_count=1, applied_source_timezones={"sensorpush": "UTC"})
    r.record_metric("heart_rate", hr, (start, end))
    r.add_excluded("non_finite_value", 2)
    r.skip_file("bad.json", "malformed content")
    r.add_unsupported_file("notes.txt")
    r.add_warning("Temperature is missing.", "temperature", "Import a SensorPush file.")
    return r


def test_import_report_round_trip_and_no_values():
    r = _report("heart_rate_2024-03-01.json", 10, T0, T0 + timedelta(hours=8))
    r.per_metric_coverage["steps"] = None
    d = r.to_dict()
    assert json.loads(json.dumps(d)) == d
    assert Import_Report.from_dict(d) == r
    # Only summary fields; no field carries telemetry values.
    assert set(d) == {
        "accepted_files", "skipped_files", "unsupported_files", "unsupported_count",
        "skipped_value_count", "skipped_row_count", "per_metric_counts", "per_metric_coverage",
        "applied_source_timezones", "discarded_duplicate_sessions", "excluded_point_counts", "warnings",
    }


def test_merge_import_reports_sums_and_unions():
    a = _report("a.json", 10, T0, T0 + timedelta(hours=1))
    b = _report("b.json", 5, T0 - timedelta(hours=1), T0 + timedelta(minutes=30))
    b.applied_source_timezones["sensorpush"] = "Europe/Berlin"
    m = a.merge(b)
    assert m.accepted_files == ["a.json", "b.json"]
    assert m.per_metric_counts == {"heart_rate": 15}
    assert m.per_metric_coverage["heart_rate"] == (T0 - timedelta(hours=1), T0 + timedelta(hours=1))
    assert m.excluded_point_counts == {"non_finite_value": 4}
    assert m.skipped_value_count == 2 and m.unsupported_count == 2
    assert m.applied_source_timezones == {"sensorpush": "Europe/Berlin"}
    assert len(m.warnings) == 2 and len(m.skipped_files) == 2
    # Inputs untouched.
    assert a.per_metric_counts == {"heart_rate": 10}
    assert merge_import_reports([]) == Import_Report()


def test_processing_report_merge_and_serialize():
    a = Processing_Report(
        removed_duplicates={("fitbit", "heart_rate"): 3},
        gaps=[{"metric": "heart_rate", "last_value_at": T0, "elapsed": timedelta(minutes=15)}],
        unavailable_metrics=["pressure"],
    )
    a.add_warning("Heart rate has 1 gap totalling 15 minutes.", "heart_rate")
    b = Processing_Report(removed_duplicates={("fitbit", "heart_rate"): 2}, unavailable_metrics=["pressure", "humidity"])
    m = merge_processing_reports([a, b])
    assert m.removed_duplicates == {("fitbit", "heart_rate"): 5}
    assert m.unavailable_metrics == ["pressure", "humidity"]
    assert len(m.warnings) == 1
    d = m.to_dict()
    assert json.loads(json.dumps(d)) == d
    assert d["gaps"][0] == {"metric": "heart_rate", "last_value_at": T0.isoformat(), "elapsed": 900.0}
    assert d["removed_duplicates"] == [{"source": "fitbit", "metric": "heart_rate", "count": 5}]
