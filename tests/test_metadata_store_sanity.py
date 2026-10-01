"""Sanity tests for the SQLite Metadata_Store (task 3.4; full tests in 3.6)."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from backend.domain.errors import PERSISTED_DATA_UNREADABLE, Import_Report, User_Error
from backend.domain.session import SleepSession
from backend.domain.stages import Sleep_Stage, Stage_Segment
from backend.persistence.metadata_store import METADATA_DB_FILENAME, SCHEMA_VERSION, FileRecord, MetadataStore

TZ = timezone(timedelta(hours=-5))
START = datetime(2024, 3, 9, 23, 0, tzinfo=TZ)
END = datetime(2024, 3, 10, 7, 0, tzinfo=TZ)


def _session() -> SleepSession:
    return SleepSession(
        start_time=START,
        end_time=END,
        source="fitbit",
        stages=(
            Stage_Segment(START, START + timedelta(hours=1), Sleep_Stage.light),
            Stage_Segment(START + timedelta(hours=1), END, Sleep_Stage.deep, is_brief_awakening=False),
        ),
        log_id="123",
        is_main_sleep=True,
        session_hrv=41.5,
        source_file="sleep-2024-03-10.json",
    )


def test_import_record_round_trip_and_persists_across_reopen(tmp_path) -> None:
    report = Import_Report(accepted_files=["sleep-2024-03-10.json"], per_metric_counts={"heart_rate": 3})
    with MetadataStore(tmp_path) as store:
        store.record_import(
            "imp-1",
            "fitbit",
            [FileRecord("sleep-2024-03-10.json", date(2024, 3, 10), {"file_type": "sleep"}), "extra.csv"],
            telemetry_file="telemetry/imp-1.json",
            import_report=report,
            sessions=[_session()],
        )
    assert (tmp_path / METADATA_DB_FILENAME).is_file()

    with MetadataStore(tmp_path) as store:
        record = store.get_import("imp-1")
        assert record is not None
        assert record.source_identifier == "fitbit"
        assert record.source_files == ["sleep-2024-03-10.json", "extra.csv"]
        assert record.file_dates == {"sleep-2024-03-10.json": date(2024, 3, 10)}
        assert record.file_metadata["sleep-2024-03-10.json"] == {"file_type": "sleep", "file_date": "2024-03-10"}
        assert record.telemetry_file == "telemetry/imp-1.json"
        assert record.import_report == report
        [(import_id, session)] = store.list_candidate_sessions()
        assert import_id == "imp-1"
        assert session == _session()
        assert [r.import_id for r in store.list_imports()] == ["imp-1"]
        assert store.delete_import("imp-1") is True
        assert store.list_imports() == [] and store.list_candidate_sessions() == []


def test_failed_transaction_leaves_prior_rows_unchanged(tmp_path) -> None:
    with MetadataStore(tmp_path) as store:
        store.record_import("imp-1", "fitbit", ["a.json"])
        store.set_setting("target_duration", 180)
        with pytest.raises(RuntimeError):
            with store.transaction():
                store.record_import("imp-2", "sensorpush", ["b.csv"])
                store.delete_import("imp-1")
                store.set_setting("target_duration", 30)
                raise RuntimeError("simulated failure")
        assert store.list_import_ids() == ["imp-1"]
        assert store.get_setting("target_duration") == 180
        with pytest.raises(ValueError):
            store.record_import("imp-1", "fitbit", ["other.json"])
        assert store.get_import("imp-1").source_files == ["a.json"]


def test_selection_settings_and_replays(tmp_path) -> None:
    with MetadataStore(tmp_path) as store:
        assert store.get_selected_session() is None
        store.set_selected_session(_session(), "auto")
        store.set_selected_session(_session(), "manual")
        selected = store.get_selected_session()
        assert selected.kind == "manual" and selected.session == _session()
        with pytest.raises(ValueError):
            store.set_selected_session(_session(), "bogus")  # type: ignore[arg-type]

        store.set_settings({"a": {"x": [1, 2.5]}, "b": "text"})
        assert store.get_settings() == {"a": {"x": [1, 2.5]}, "b": "text"}

        rec = store.record_replay(
            "rep-1", session_start=START, session_end=END, target_duration_s=180, wav_file="replays/rep-1.wav"
        )
        assert store.list_replays() == [rec]
        with pytest.raises(ValueError):
            store.record_replay("rep-2", session_start=START, session_end=END, target_duration_s=180, wav_file="../x.wav")


def test_corrupt_database_is_unreadable_and_file_is_released(tmp_path) -> None:
    db = tmp_path / METADATA_DB_FILENAME
    db.write_bytes(b"this is not a sqlite database" * 10)
    with pytest.raises(User_Error) as info:
        MetadataStore(tmp_path)
    assert info.value.code == PERSISTED_DATA_UNREADABLE
    db.unlink()  # the failed open closed its connection (matters on Windows)

    store = MetadataStore(tmp_path)
    store.close()
    store.close()
    db.unlink()


def test_schema_one_migrates_without_losing_imports_and_context_round_trips(tmp_path):
    import sqlite3
    with MetadataStore(tmp_path) as store:
        store.record_import("prior", "fitbit", ["sleep.json"], sessions=[_session()])
        store.set_setting("keep", 180)
    with sqlite3.connect(tmp_path / METADATA_DB_FILENAME) as conn:
        conn.execute("DROP TABLE import_context")
        conn.execute("PRAGMA user_version = 1")
    context = {"point_files": ["heart_rate.json"], "session_hrv": [{"date": "2024-03-10", "rmssd": 42}]}
    with MetadataStore(tmp_path) as store:
        assert store.list_import_ids() == ["prior"]
        assert store.get_import_context("prior") == {}
        assert store.get_setting("keep") == 180
        assert store.list_candidate_sessions()[0][1] == _session()
        store.record_import("new", "fitbit", ["heart_rate.json"], context=context)
    with MetadataStore(tmp_path) as store:
        assert store.get_import_context("new") == context
        assert store._connection().execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        store.delete_import("new")
        assert store.get_import_context("new") == {}
