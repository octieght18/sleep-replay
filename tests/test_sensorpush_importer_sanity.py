"""Sanity checks for the SensorPush_Importer adapter (full coverage lands with task 8.5)."""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from backend.domain import units as du
from backend.domain.adapter import Data_Source_Adapter, TimezoneContext, implements_candidate_sessions
from backend.domain.errors import NO_READABLE_ROWS, NO_TIMESTAMP_COLUMN, User_Error
from backend.domain.telemetry import Metric, is_valid
from backend.ingestion.sensorpush import SensorPushImporter
from backend.ingestion.sensorpush.importer import parse_number

BERLIN = ZoneInfo("Europe/Berlin")
UTC = timezone.utc


def _write(tmp_path, name: str, text: str, bom: bool = False):
    path = tmp_path / name
    path.write_bytes((b"\xef\xbb\xbf" if bom else b"") + text.encode("utf-8"))
    return path


def test_adapter_shape() -> None:
    importer = SensorPushImporter()
    assert isinstance(importer, Data_Source_Adapter)
    assert importer.source_identifier == "sensorpush"
    assert not implements_candidate_sessions(importer)


def test_load_points_report_and_skips(tmp_path) -> None:
    path = _write(
        tmp_path,
        "sp.csv",
        "SensorId,Observed,Temperature (°F),Relative Humidity (%),Dew Point (°F)\n"
        "A1,2024-03-01 23:00,68.0,41.0,43.1\n"
        ",2024-03-01 23:01,,42.5,43.0\n"  # empty temp -> skipped value; empty sensor id
        "A1,not a time,68.1,41.0,43.0\n"  # bad timestamp -> skipped row
        "\n"
        "A1,2024-03-01T23:02:00Z,abc,43.0,43.0\n",  # explicit offset; non-numeric temp
        bom=True,
    )
    result = SensorPushImporter().load([path], TimezoneContext(BERLIN))

    points = result.telemetry
    assert all(is_valid(p) for p in points)
    assert all(p.timestamp.tzinfo is UTC for p in points)
    assert len(points) == 4
    assert points[0].timestamp == datetime(2024, 3, 1, 22, 0, tzinfo=UTC)  # Berlin +01:00
    assert points[0].source == "sensorpush:A1"
    assert (points[0].metric, points[0].value, points[0].unit) == (Metric.temperature, 68.0, du.FAHRENHEIT)
    assert points[2].source == "sensorpush"  # empty sensor id cell
    assert points[3].timestamp == datetime(2024, 3, 1, 23, 2, tzinfo=UTC)

    report = result.report
    assert report.accepted_files == ["sp.csv"]
    assert report.skipped_row_count == 1
    assert report.skipped_value_count == 2
    assert report.per_metric_counts == {"temperature": 1, "humidity": 3}
    assert report.per_metric_coverage["humidity"] == (
        datetime(2024, 3, 1, 22, 0, tzinfo=UTC),
        datetime(2024, 3, 1, 23, 2, tzinfo=UTC),
    )
    assert report.applied_source_timezones == {"sensorpush": "Europe/Berlin"}
    missing = [w for w in report.warnings if "unavailable" in w.description]
    assert [w.subject for w in missing] == ["pressure"]
    assert result.sessions == []


def test_semicolon_decimal_comma_and_inference(tmp_path) -> None:
    path = _write(tmp_path, "eu.csv", "Time;Temp;Baro\n2024-03-01 23:00;21,5;1013,2\n")
    result = SensorPushImporter().load(path, TimezoneContext(ZoneInfo("UTC")))
    by_metric = {p.metric: p for p in result.telemetry}
    assert by_metric[Metric.temperature].value == 21.5
    assert by_metric[Metric.temperature].unit == du.CELSIUS
    assert by_metric[Metric.pressure].unit == du.HPA
    inferred = [w for w in result.report.warnings if "inferred" in w.description]
    assert {w.subject for w in inferred} == {"temperature", "pressure"}


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("Temp,Humidity\n20,40\n", NO_TIMESTAMP_COLUMN),
        ("Time,Battery\n2024-03-01 23:00,90\n", NO_TIMESTAMP_COLUMN),
        ("Time,Temp (C)\n", NO_READABLE_ROWS),
        ("Time,Temp (C)\nbad,20\n2024-03-01 23:00,\n", NO_READABLE_ROWS),
    ],
)
def test_whole_file_errors(tmp_path, text: str, code: str) -> None:
    path = _write(tmp_path, "bad.csv", text)
    with pytest.raises(User_Error) as exc:
        SensorPushImporter().load([path], TimezoneContext(BERLIN))
    assert exc.value.code == code
    assert exc.value.file_name == "bad.csv"


def test_failed_file_is_skipped_when_another_succeeds(tmp_path) -> None:
    good = _write(tmp_path, "good.csv", "Time,Temp (C)\n2024-03-01 23:00,20\n")
    bad = _write(tmp_path, "bad.csv", "Temp,Humidity\n20,40\n")
    result = SensorPushImporter().load([bad, good], TimezoneContext(BERLIN))
    assert result.report.accepted_files == ["good.csv"]
    assert [name for name, _ in result.report.skipped_files] == ["bad.csv"]
    assert len(result.telemetry) == 1


@pytest.mark.parametrize(
    ("text", "delimiter", "expected"),
    [("21.5", ",", 21.5), ("-3", ",", -3.0), ("21,5", ";", 21.5), ("21,5", ",", None),
     ("", ",", None), ("nan", ",", None), ("inf", ",", None), ("1e999", ",", None), ("1_000", ",", None)],
)
def test_parse_number(text: str, delimiter: str, expected: float | None) -> None:
    assert parse_number(text, delimiter) == expected
