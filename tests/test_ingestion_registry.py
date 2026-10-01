"""Sanity tests for the adapter registry and atomic import (task 5.1).

Requirements 1.7, 7.2, 7.4, 7.6, 7.7, 9.10, 16.5. The validation-gate property
test lives in task 5.2.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from backend.domain.adapter import LoadResult, TimezoneContext
from backend.domain.errors import (
    INVALID_TIMEZONE,
    NO_READABLE_ROWS,
    SAVE_FAILED,
    SOURCE_LOAD_FAILED,
    Import_Report,
    User_Error,
)
from backend.domain.session import SleepSession
from backend.domain.telemetry import Metric, TelemetryPoint
from backend.ingestion.registry import (
    EXCLUDED_INCONVERTIBLE_UNIT,
    EXCLUDED_NAIVE_TIMESTAMP,
    EXCLUDED_NON_FINITE_VALUE,
    EXCLUDED_NON_MVP_METRIC,
    AdapterRegistry,
    ImportRecord,
    ImportStores,
    UnknownSourceError,
    apply_validation_gate,
)
from backend.persistence.telemetry_store import TelemetryStore

T0 = datetime(2024, 1, 1, 22, 0, tzinfo=timezone(timedelta(hours=1)))


def _pt(i: int = 0, **overrides) -> TelemetryPoint:
    fields = dict(timestamp=T0 + timedelta(minutes=i), source="fake", metric=Metric.heart_rate, value=60 + i, unit="bpm")
    fields.update(overrides)
    return TelemetryPoint(**fields)


class FakeAdapter:
    """Load-only adapter returning a fixed list of points (no candidate_sessions)."""

    source_identifier = "fake"

    def __init__(self, points=(), error: Exception | None = None) -> None:
        self.points = list(points)
        self.error = error
        self.calls: list[TimezoneContext] = []

    def load(self, source, tz_context):
        self.calls.append(tz_context)
        if self.error is not None:
            raise self.error
        report = Import_Report(accepted_files=["a.csv"])
        report.record_metric("heart_rate", 999, None)  # adapter's count is replaced by the gate
        return LoadResult(telemetry=list(self.points), report=report)


class SessionAdapter(FakeAdapter):
    source_identifier = "sessions"

    def candidate_sessions(self, source, tz_context):
        return [SleepSession(start_time=T0, end_time=T0 + timedelta(hours=8), source="sessions")]


class Recorder:
    def __init__(self, error: Exception | None = None) -> None:
        self.records: list[ImportRecord] = []
        self.error = error

    def __call__(self, record: ImportRecord) -> None:
        if self.error is not None:
            raise self.error
        self.records.append(record)


def _stores(data_dir: Path, recorder: Recorder | None = None) -> ImportStores:
    return ImportStores(telemetry=TelemetryStore(data_dir), record_import=recorder or Recorder())


def _files(data_dir: Path) -> list[Path]:
    return [p for p in data_dir.rglob("*") if p.is_file()]


# ---------------------------------------------------------------------------


def test_register_and_lookup() -> None:
    reg = AdapterRegistry()
    adapter = FakeAdapter()
    reg.register(adapter)
    assert reg.get("fake") is adapter
    assert "fake" in reg and reg.source_identifiers() == ("fake",)
    with pytest.raises(ValueError):
        reg.register(FakeAdapter())  # duplicate identifier
    with pytest.raises(UnknownSourceError):
        reg.get("nope")
    with pytest.raises(TypeError):
        reg.register(object())  # type: ignore[arg-type]


def test_gate_counts_each_reason_and_keeps_order() -> None:
    good = [_pt(0), _pt(1, metric=Metric.temperature, unit="°F", value=98.6), _pt(2)]
    bad = [
        _pt(3, timestamp=datetime(2024, 1, 1, 22, 3)),
        _pt(4, metric="sleep_stage"),
        _pt(5, value=float("nan")),
        _pt(6, value=float("inf")),
        _pt(7, unit="mmHg"),
        # Sub-second UTC offset: not persistable exactly, counted as naive_timestamp.
        _pt(8, timestamp=datetime(2024, 1, 1, 22, 8, tzinfo=timezone(timedelta(microseconds=1)))),
    ]
    gate = apply_validation_gate([bad[0], good[0], bad[1], good[1], bad[2], bad[3], good[2], bad[4], bad[5]])
    assert list(gate.retained) == good
    assert dict(gate.excluded) == {
        EXCLUDED_NAIVE_TIMESTAMP: 2,
        EXCLUDED_NON_MVP_METRIC: 1,
        EXCLUDED_NON_FINITE_VALUE: 2,
        EXCLUDED_INCONVERTIBLE_UNIT: 1,
    }


def test_run_import_persists_valid_points_and_records(tmp_data_dir: Path) -> None:
    reg = AdapterRegistry()
    points = [_pt(0), _pt(1, value=float("nan")), _pt(2)]
    reg.register(FakeAdapter(points))
    recorder = Recorder()
    stores = _stores(tmp_data_dir, recorder)

    result = reg.run_import("fake", ["x/a.csv"], {"sensorpush": "Europe/Berlin"}, "UTC", stores)

    assert [r.import_id for r in recorder.records] == [result.import_id]
    record = recorder.records[0]
    assert record.source_files == ("a.csv",)
    assert record.point_count == 2 and record.candidate_sessions == ()  # no candidate_sessions => zero
    assert record.source_timezone_overrides == {"sensorpush": "Europe/Berlin"}
    assert result.report.excluded_point_counts == {EXCLUDED_NON_FINITE_VALUE: 1}
    assert result.report.per_metric_counts == {"heart_rate": 2}
    assert result.report.per_metric_coverage["heart_rate"] == (T0, T0 + timedelta(minutes=2))
    loaded = stores.telemetry.load(result.import_id)
    assert [p.value for p in loaded] == [60, 62]


def test_candidate_sessions_are_collected(tmp_data_dir: Path) -> None:
    reg = AdapterRegistry()
    reg.register(SessionAdapter([_pt(0, source="sessions")]))
    result = reg.run_import("sessions", ["s.json"], None, ZoneInfo("UTC"), _stores(tmp_data_dir))
    assert len(result.candidate_sessions) == 1


def test_invalid_override_rejected_before_load(tmp_data_dir: Path) -> None:
    reg = AdapterRegistry()
    adapter = reg.register(FakeAdapter([_pt()]))
    with pytest.raises(User_Error) as info:
        reg.run_import("fake", ["a.csv"], {"sensorpush": "Mars/Olympus"}, "UTC", _stores(tmp_data_dir))
    assert info.value.code == INVALID_TIMEZONE
    assert adapter.calls == []
    assert _files(tmp_data_dir) == []


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (User_Error(NO_READABLE_ROWS, "No rows.", "Fix it.", file_name="a.csv"), NO_READABLE_ROWS),
        (RuntimeError("boom"), SOURCE_LOAD_FAILED),
    ],
)
def test_whole_source_failure_stores_nothing(tmp_data_dir: Path, error: Exception, code: str) -> None:
    reg = AdapterRegistry()
    reg.register(FakeAdapter(error=error))
    recorder = Recorder()
    with pytest.raises(User_Error) as info:
        reg.run_import("fake", ["dir/a.csv"], None, "UTC", _stores(tmp_data_dir, recorder))
    assert info.value.code == code
    assert info.value.file_name == "a.csv"
    assert recorder.records == [] and _files(tmp_data_dir) == []


@pytest.mark.parametrize("error", [RuntimeError("db locked"), User_Error(SAVE_FAILED, "Nope.", "Retry.")])
def test_record_failure_removes_telemetry_and_keeps_earlier_imports(tmp_data_dir: Path, error: Exception) -> None:
    reg = AdapterRegistry()
    reg.register(FakeAdapter([_pt(0)]))
    earlier = reg.run_import("fake", ["a.csv"], None, "UTC", _stores(tmp_data_dir))
    before = {p: p.read_bytes() for p in _files(tmp_data_dir)}

    with pytest.raises(User_Error) as info:
        reg.run_import("fake", ["b.csv"], None, "UTC", _stores(tmp_data_dir, Recorder(error)))
    assert info.value.code == SAVE_FAILED
    assert {p: p.read_bytes() for p in _files(tmp_data_dir)} == before
    assert TelemetryStore(tmp_data_dir).list_import_ids() == [earlier.import_id]


def test_telemetry_write_failure_records_nothing(tmp_data_dir: Path) -> None:
    def failing_writer(fh, data):
        raise OSError("disk full")

    reg = AdapterRegistry()
    reg.register(FakeAdapter([_pt(0)]))
    recorder = Recorder()
    stores = ImportStores(TelemetryStore(tmp_data_dir, writer=failing_writer), recorder)
    with pytest.raises(User_Error) as info:
        reg.run_import("fake", ["a.csv"], None, "UTC", stores)
    assert info.value.code == SAVE_FAILED
    assert recorder.records == []
    assert TelemetryStore(tmp_data_dir).list_import_ids() == []
