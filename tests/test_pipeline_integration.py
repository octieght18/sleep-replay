"""Service integration: restart, offline use, storage boundary, and atomicity."""
import json
import socket
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from backend.api.pipeline import Pipeline
from backend.domain.adapter import LoadResult
from backend.domain.errors import User_Error, Import_Report
from backend.domain.session import SleepSession
from backend.domain.stages import Sleep_Stage, Stage_Segment
from backend.domain.telemetry import Metric, TelemetryPoint
from backend.domain.timezones import FITBIT_SLEEP, FITBIT_HEART_RATE

SAMPLE_DIR = Path(__file__).resolve().parents[1] / "sample_data"
T0 = datetime(2024, 3, 1, 22, tzinfo=timezone.utc)


class FakeAdapter:
    source_identifier = "test_adapter"

    def __init__(self, points=(), sessions=()):
        self.points, self.sessions = list(points), list(sessions)

    def load(self, source, tz_context):
        return LoadResult(telemetry=self.points, report=Import_Report(accepted_files=["synthetic.csv"]))

    def candidate_sessions(self, source, tz_context):
        return self.sessions


def assert_error(error, code, file=False):
    assert error.code == code and error.description and error.action
    assert not any(s in error.description for s in ("Traceback", "ValueError", "OSError", ".py:"))
    if file:
        assert error.file_name


def test_sample_end_to_end_and_restart(tmp_data_dir, monkeypatch):
    monkeypatch.setenv("SLEEP_REPLAY_DISPLAY_TIMEZONE", "Asia/Tokyo")
    with Pipeline() as pipeline:
        report, candidates = pipeline.use_sample_data()
        assert len(candidates) == 1
        assert candidates[0].end_time - candidates[0].start_time == timedelta(hours=8)
        assert not report.skipped_files and not report.warnings and not report.unsupported_files
        assert report.skipped_row_count == report.skipped_value_count == 0
        result = pipeline.process(180)
        assert result.compression.ratio == 160 and len(result.features) == len(result.coarse_states) == 360
        assert result.events
        selection = pipeline.metadata_store.get_selected_session()
        assert selection.kind == "auto"
        records = pipeline.metadata_store.list_imports()
        assert len(records[0].import_report.accepted_files) == 4
        assert records[1].import_report.accepted_files == ["sensorpush.csv"]
        assert records[1].import_report.per_metric_counts == {"temperature": 450, "humidity": 450, "pressure": 450}
    with Pipeline() as reopened:
        again = reopened.process(180)
        assert again == result


def test_adapter_extensibility_without_processing_changes(tmp_data_dir):
    end = T0 + timedelta(hours=1)
    session = SleepSession(T0, end, "test_adapter", (Stage_Segment(T0, end, Sleep_Stage.light),), is_main_sleep=True)
    points = [TelemetryPoint(T0 + timedelta(minutes=i), "test_adapter", Metric.heart_rate, 60 + i % 3, "bpm")
              for i in range(60)]
    with Pipeline() as pipeline:
        pipeline.registry.register(FakeAdapter(points, [session]))
        report, candidates = pipeline.import_files("test_adapter", ["synthetic.csv"])
        assert candidates == [session] and pipeline.selected_session.telemetry == tuple(points)
        result = pipeline.process(30)
        assert len(result.features) == 60 and result.timeline.values[Metric.heart_rate][0] == 60
        assert [e.type.value for e in result.events] == ["sleep onset", "awakening"]


def test_offline_pipeline_matches_online(tmp_data_dir, monkeypatch):
    with Pipeline(tmp_data_dir / "normal") as pipeline:
        report, _ = pipeline.use_sample_data()
        expected = pipeline.process(180)
    def no_network(*args, **kwargs):
        raise AssertionError("pipeline attempted network access")
    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket.socket, "connect_ex", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    monkeypatch.setattr(socket, "getaddrinfo", no_network)
    for name in ("FITBIT_CLIENT_ID", "FITBIT_CLIENT_SECRET", "GOOGLE_APPLICATION_CREDENTIALS", "SENSORPUSH_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    with Pipeline(tmp_data_dir / "offline") as pipeline:
        offline_report, _ = pipeline.use_sample_data()
        assert offline_report == report and pipeline.process(180) == expected


def test_pipeline_writes_stay_in_data_directory_and_sample_unchanged(tmp_data_dir, monkeypatch):
    before = {p: p.read_bytes() for p in SAMPLE_DIR.iterdir()}
    monkeypatch.chdir(tmp_data_dir.parent)
    written = []
    # Capture actual Python-level writes rather than assuming directory listings prove containment.
    import sys
    active = True
    def audit(event, args):
        if active and event == "open" and isinstance(args[0], (str, bytes)):
            mode, flags = args[1], args[2]
            if (mode and any(c in mode for c in "wax+")) or flags & (0x40 | 0x200 | 0x400):
                written.append(Path(args[0]).resolve())
    sys.addaudithook(audit)
    with Pipeline() as pipeline:
        pipeline.use_sample_data()
        pipeline.process(180)
        assert pipeline.metadata_store.path.is_relative_to(tmp_data_dir)
    active = False
    assert written and all(p.is_relative_to(tmp_data_dir) for p in written)
    assert {p: p.read_bytes() for p in SAMPLE_DIR.iterdir()} == before
    assert not list(tmp_data_dir.rglob("*.tmp"))


def test_failed_post_import_discovery_rolls_back_metadata_and_telemetry(tmp_data_dir, monkeypatch):
    with Pipeline() as pipeline:
        pipeline.use_sample_data()
        previous = pipeline.process(180)
        ids = pipeline.metadata_store.list_import_ids()
        files = {p: p.read_bytes() for p in (tmp_data_dir / "telemetry").iterdir()}
        selected = pipeline.metadata_store.get_selected_session()
        pipeline.metadata_store.set_setting("keep", {"value": 1})
        original = pipeline._discover
        def fail(*args):
            raise User_Error("SAVE_FAILED", "The selection could not be saved.", "Try again.")
        monkeypatch.setattr(pipeline, "_discover", fail)
        with pytest.raises(User_Error) as exc:
            pipeline.import_files("sensorpush", [SAMPLE_DIR / "sensorpush.csv"])
        assert_error(exc.value, "SAVE_FAILED")
        assert pipeline.metadata_store.list_import_ids() == ids
        assert pipeline.metadata_store.get_selected_session() == selected
        assert pipeline.metadata_store.get_setting("keep") == {"value": 1}
        assert {p: p.read_bytes() for p in (tmp_data_dir / "telemetry").iterdir()} == files
        monkeypatch.setattr(pipeline, "_discover", original)
        assert pipeline.process(180) == previous


def test_sample_batch_failure_rolls_back_first_source(tmp_data_dir, monkeypatch):
    with Pipeline() as pipeline:
        sensor = pipeline.registry.get("sensorpush")
        def fail(*args):
            raise User_Error("SOURCE_LOAD_FAILED", "Cannot read the room export.", "Choose a valid CSV.", file_name="sensorpush.csv")
        monkeypatch.setattr(sensor, "load", fail)
        with pytest.raises(User_Error):
            pipeline.use_sample_data()
        assert pipeline.metadata_store.list_imports() == []
        assert pipeline.telemetry_store.list_import_ids() == []
        assert pipeline.selected_session is None


def test_no_candidate_keeps_telemetry_and_manual_range_recovers(tmp_data_dir):
    with Pipeline() as pipeline:
        with pytest.raises(User_Error) as exc:
            pipeline.import_files("sensorpush", [SAMPLE_DIR / "sensorpush.csv"])
        assert_error(exc.value, "NO_SLEEP_SESSION")
        assert len(pipeline.metadata_store.list_imports()) == 1
        pipeline.define_manual_range("2024-03-01T22:00:00-05:00", "2024-03-02T06:00:00-05:00")
        result = pipeline.process(180)
        assert not result.session.has_stage_data and result.events
        assert pipeline.metadata_store.get_selected_session().kind == "manual"


def test_processing_and_invalid_manual_range_leave_selection_and_settings_unchanged(tmp_data_dir):
    with Pipeline() as pipeline:
        pipeline.use_sample_data()
        pipeline.metadata_store.set_setting("target_duration", 180)
        selected = pipeline.metadata_store.get_selected_session()
        ids = pipeline.metadata_store.list_import_ids()
        for start, end in ((None, None), ("bad", "bad"), (T0, T0), (T0, T0 + timedelta(hours=25))):
            with pytest.raises(User_Error) as exc:
                pipeline.define_manual_range(start, end)
            assert_error(exc.value, "INVALID_MANUAL_RANGE")
        for target in (0, 31, True, "180"):
            with pytest.raises(User_Error) as exc:
                pipeline.process(target)
            assert_error(exc.value, "INVALID_TARGET_DURATION")
        assert pipeline.metadata_store.get_selected_session() == selected
        assert pipeline.metadata_store.list_import_ids() == ids
        assert pipeline.metadata_store.get_setting("target_duration") == 180


def test_corrupt_persisted_telemetry_prevents_restart_and_rolls_back_new_import(tmp_data_dir):
    with Pipeline() as pipeline:
        pipeline.use_sample_data()
        ids = pipeline.metadata_store.list_import_ids()
        pipeline.telemetry_store.path_for(ids[0]).write_bytes(b"broken")
        with pytest.raises(User_Error) as exc:
            pipeline.import_files("sensorpush", [SAMPLE_DIR / "sensorpush.csv"])
        assert_error(exc.value, "PERSISTED_DATA_UNREADABLE", file=True)
        assert pipeline.metadata_store.list_import_ids() == ids
        assert pipeline.telemetry_store.list_import_ids() == sorted(ids)
    with pytest.raises(User_Error) as exc:
        Pipeline()
    assert_error(exc.value, "PERSISTED_DATA_UNREADABLE", file=True)


def test_telemetry_only_empty_source_and_insufficient_session_errors(tmp_data_dir):
    with Pipeline() as pipeline:
        pipeline.registry.register(FakeAdapter())
        with pytest.raises(User_Error) as exc:
            pipeline.import_files("test_adapter", ["empty.csv"])
        assert_error(exc.value, "SOURCE_LOAD_FAILED", file=True)
        assert not pipeline.metadata_store.list_imports()
        pipeline.define_manual_range(T0, T0 + timedelta(hours=1))
        with pytest.raises(User_Error) as exc:
            pipeline.process(30)
        assert_error(exc.value, "INSUFFICIENT_DATA")


def test_partial_import_keeps_valid_files(tmp_data_dir):
    bad = tmp_data_dir / "sleep-2024-03-03.json"
    bad.write_text("{malformed")
    with Pipeline() as pipeline:
        report, candidates = pipeline.import_files("fitbit", [SAMPLE_DIR / "sleep-2024-03-01.json", bad],
                                                    {FITBIT_SLEEP: "America/New_York"})
        assert len(candidates) == 1 and len(report.skipped_files) == 1
        assert report.skipped_files[0][0] == bad.name
        assert "sleep-2024-03-01.json" in report.accepted_files
        assert pipeline.process(30).events


def test_file_date_filter_and_daily_hrv_survive_restart(tmp_data_dir):
    # Both telemetry files contain in-session instants, but only one filename is in the date window.
    sleep = tmp_data_dir / "sleep-2024-03-01.json"
    sleep.write_text(json.dumps([{"startTime": T0.isoformat(), "endTime": (T0 + timedelta(hours=8)).isoformat(),
                                  "isMainSleep": True, "levels": {"data": [{"dateTime": T0.isoformat(),
                                  "seconds": 28800, "level": "light"}]}}]))
    paths = [sleep]
    for day, value in (("2024-03-01", 60), ("2024-04-01", 120)):
        path = tmp_data_dir / f"heart_rate-{day}.json"
        path.write_text(json.dumps([{"dateTime": "03/01/24 22:00:00", "value": {"bpm": value}}]))
        paths.append(path)
    summary = tmp_data_dir / "Daily Heart Rate Variability Summary - 2024-03.csv"
    summary.write_text("timestamp,rmssd\n2024-03-02,40\n2024-03-02,60\n2024-03-01,90\n")
    paths.append(summary)
    with Pipeline() as pipeline:
        pipeline.import_files("fitbit", paths, {FITBIT_HEART_RATE: "UTC"})
        assert [p.value for p in pipeline.selected_session.telemetry] == [60]
        assert pipeline.selected_session.session_hrv == 50
    with Pipeline() as pipeline:
        assert [p.value for p in pipeline.selected_session.telemetry] == [60]
        assert pipeline.selected_session.session_hrv == 50


def test_user_selection_persists_and_follows_overlap_merge(tmp_data_dir):
    def session(start, end, stage):
        return SleepSession(start, end, "test_adapter", (Stage_Segment(start, end, stage),))
    older = session(T0, T0 + timedelta(hours=1), Sleep_Stage.light)
    later = session(T0 + timedelta(days=1), T0 + timedelta(days=1, hours=1), Sleep_Stage.rem)
    adapter = FakeAdapter(sessions=[older, later])
    with Pipeline() as pipeline:
        pipeline.registry.register(adapter)
        pipeline.import_files("test_adapter", ["synthetic.csv"])
        assert pipeline.selected_session.start_time == later.start_time
        pipeline.define_manual_range(T0, T0 + timedelta(minutes=30))
        pipeline.select_session(older)
        adapter.sessions = [session(T0 + timedelta(days=2), T0 + timedelta(days=2, hours=1), Sleep_Stage.deep)]
        pipeline.import_files("test_adapter", ["later.csv"])
        assert pipeline.selected_session.start_time == older.start_time
        adapter.sessions = [session(T0 + timedelta(minutes=30), T0 + timedelta(minutes=90), Sleep_Stage.deep)]
        pipeline.import_files("test_adapter", ["overlap.csv"])
        assert pipeline.selected_session.end_time == T0 + timedelta(minutes=90)
        assert pipeline.metadata_store.get_selected_session().kind == "user"
    with Pipeline() as pipeline:
        assert pipeline.selected_session.end_time == T0 + timedelta(minutes=90)
