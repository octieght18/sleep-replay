"""Remaining importer packaging and alignment edge cases from tasks 7.8, 8.5, 11.8."""
import io
import zipfile
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from backend.domain.adapter import TimezoneContext
from backend.domain.errors import Import_Report, User_Error
from backend.domain.session import SleepSession
from backend.domain.stages import Sleep_Stage, Stage_Segment
from backend.domain.telemetry import Metric, TelemetryPoint
from backend.ingestion.fitbit import packaging
from backend.ingestion.sensorpush.importer import SensorPushImporter
from backend.processing.aligner import align
from backend.processing.compression import compression_ratio


@pytest.mark.parametrize("name", ["/absolute/SLEEP-2024-03-01.JSON", "../../escape/sleep-2024-03-01.json", "nested/a/sleep-2024-03-01.json"])
def test_archive_paths_are_read_without_extraction(tmp_data_dir, name):
    archive = tmp_data_dir / "export.ZIP"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr(name, b"[]")
    files = packaging.load_fitbit_files(archive, Import_Report())
    assert [(f.name, f.data) for f in files] == [(name, b"[]")]
    assert list(tmp_data_dir.iterdir()) == [archive]


def test_actual_decompressed_limit_is_enforced_even_when_declared_size_is_small(monkeypatch):
    monkeypatch.setattr(packaging, "MAX_ARCHIVE_MEMBER_SIZE", 10)
    info = zipfile.ZipInfo("sleep-2024-03-01.json")
    info.file_size = 1
    class Archive:
        def open(self, info):
            return io.BytesIO(b"x" * 100)
    with pytest.raises(packaging.MemberSkipped) as exc:
        packaging._read_member(Archive(), info)
    assert "10 bytes" in exc.value.reason


@pytest.mark.parametrize("kind", ["encrypted", "broken-codec"])
def test_unreadable_member_skips_and_remaining_archive_loads(tmp_data_dir, monkeypatch, kind):
    archive = tmp_data_dir / "export.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("steps-2024-03-01.json", b"[]")
        zf.writestr("sleep-2024-03-01.json", b"[]")
    original = zipfile.ZipFile.open
    def unreadable(self, name, *args, **kwargs):
        if name.filename.startswith("steps"):
            if kind == "encrypted":
                raise RuntimeError("password needed")
            raise zipfile.BadZipFile("bad CRC")
        return original(self, name, *args, **kwargs)
    monkeypatch.setattr(zipfile.ZipFile, "open", unreadable)
    report = Import_Report()
    files = packaging.load_fitbit_files(archive, report)
    assert [f.name for f in files] == ["sleep-2024-03-01.json"]
    assert report.skipped_files[0][0] == "steps-2024-03-01.json"
    assert ("password-protected" if kind == "encrypted" else "decompressed") in report.skipped_files[0][1]


@pytest.mark.parametrize("delimiter", [",", ";"])
@pytest.mark.parametrize("bom", [False, True])
def test_sensorpush_delimiter_bom_matrix(tmp_data_dir, delimiter, bom):
    path = tmp_data_dir / "room.csv"
    rows = [["Observed", "Temperature (C)", "RH (%)", "Baro (hPa)", "Sensor ID"],
            ["2024-03-01 22:00", "20", "45", "1010", "bedroom"]]
    content = "\n".join(delimiter.join(row) for row in rows) + "\n\n"
    path.write_bytes((b"\xef\xbb\xbf" if bom else b"") + content.encode("utf-8"))
    result = SensorPushImporter().load(path, TimezoneContext(ZoneInfo("UTC")))
    assert len(result.telemetry) == 3 and all(p.source == "sensorpush:bedroom" for p in result.telemetry)
    assert not result.report.warnings and result.report.skipped_value_count == result.report.skipped_row_count == 0


@pytest.mark.parametrize("timestamp", ["2024-03-01 22:00", "2024-03-01T22:00:00Z", "2024-03-01T23:00+01:00",
                                        "3/1/2024 10:00 PM", "3/1/2024 10:00:00 PM", "3/1/2024 22:00", "3/1/2024 22:00:00"])
def test_sensorpush_supported_timestamp_formats_end_to_end(tmp_data_dir, timestamp):
    path = tmp_data_dir / "room.csv"
    path.write_text(f"Time,Temp C,Humidity %,Pressure hPa\n{timestamp},20,40,1010\n")
    result = SensorPushImporter().load(path, TimezoneContext(ZoneInfo("UTC")))
    assert all(p.timestamp == datetime(2024, 3, 1, 22, tzinfo=timezone.utc) for p in result.telemetry)


def test_sensorpush_malformed_csv_is_structured_file_error(tmp_data_dir):
    path = tmp_data_dir / "bad.csv"
    path.write_text('Time,Temp C\n"2024-03-01 22:00,20\n')
    with pytest.raises(User_Error) as exc:
        SensorPushImporter().load(path, TimezoneContext(ZoneInfo("UTC")))
    assert exc.value.file_name == "bad.csv" and exc.value.action
    assert "Traceback" not in exc.value.description


@pytest.mark.parametrize(("date", "hours"), [("2024-11-02", 9), ("2024-03-09", 7)])
def test_alignment_and_compression_across_dst_transition(date, hours):
    zone = ZoneInfo("America/New_York")
    start = datetime.fromisoformat(date + "T22:00").replace(tzinfo=zone)
    end = (start.replace(tzinfo=None) + timedelta(hours=8)).replace(tzinfo=zone)
    stages = (Stage_Segment(start, end, Sleep_Stage.deep),)
    session = SleepSession(start, end, "fitbit", stages)
    timeline, _ = align(session)
    assert timeline.sample_count == hours * 60
    assert timeline.end_time - timeline.start_time == timedelta(hours=hours)
    assert compression_ratio(session, 180) == hours * 3600 / 180
    assert all(timeline.timestamps[i + 1] - timeline.timestamps[i] == timedelta(seconds=60)
               for i in range(timeline.sample_count - 1))


def test_duplicate_conversion_precedes_sensor_tie_selection():
    start = datetime(2024, 3, 1, tzinfo=timezone.utc)
    end = start + timedelta(minutes=3)
    points = [TelemetryPoint(start, "a", Metric.temperature, 68, "°F"),
              TelemetryPoint(start, "a", Metric.temperature, 22, "°C"),
              TelemetryPoint(start + timedelta(minutes=1), "a", Metric.temperature, 23, "°C"),
              TelemetryPoint(start, "b", Metric.temperature, 50, "°C"),
              TelemetryPoint(start + timedelta(minutes=1), "b", Metric.temperature, 50, "°C")]
    timeline, report = align(SleepSession(start, end, "fitbit", telemetry=tuple(points)))
    assert timeline.values[Metric.temperature][:2] == (21, 23)
    assert report.removed_duplicates == {("a", "temperature"): 1}
    assert report.sensor_selection == [{"metric": "temperature", "selected": "a", "ignored": ["b"]}]
