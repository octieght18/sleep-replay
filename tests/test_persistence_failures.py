"""Startup failure, exact atomic cleanup, corrupt stored points, and private logging."""
import io
import json
import subprocess
import sys
from datetime import datetime, timezone

import pytest

from backend.domain.errors import User_Error
from backend.domain.telemetry import Metric, TelemetryPoint
from backend.persistence.data_directory import prepare_data_dir, prepare_data_dir_or_exit
from backend.persistence.safe_logging import configure_logging, get_logger, shutdown_logging, LogFieldError
from backend.persistence.telemetry_store import TelemetryStore


def test_env_override_and_unwritable_startup_exit(tmp_data_dir, monkeypatch):
    assert prepare_data_dir() == tmp_data_dir
    import backend.persistence.data_directory as directory
    def fail(*args, **kwargs):
        raise PermissionError("blocked")
    monkeypatch.setattr(directory.tempfile, "mkstemp", fail)
    output = io.StringIO()
    with pytest.raises(SystemExit) as exc:
        prepare_data_dir_or_exit(stream=output)
    assert exc.value.code != 0 and "DATA_DIR_UNWRITABLE" in output.getvalue()
    assert str(tmp_data_dir) in output.getvalue() and "SLEEP_REPLAY_DATA_DIR" in output.getvalue()


def test_startup_exits_before_reading_input(tmp_data_dir):
    blocker = tmp_data_dir / "not-a-directory"
    blocker.write_text("keep")
    result = subprocess.run([sys.executable, "-c",
        "from backend.persistence.data_directory import prepare_data_dir_or_exit; "
        "prepare_data_dir_or_exit({'SLEEP_REPLAY_DATA_DIR': __import__('sys').argv[1]}); "
        "raise AssertionError('input was read')", str(blocker)], capture_output=True, text=True)
    assert result.returncode != 0 and "DATA_DIR_UNWRITABLE" in result.stderr
    assert "input was read" not in result.stderr and blocker.read_text() == "keep"


def test_write_failure_cleans_partial_temp_and_preserves_prior_file(tmp_data_dir):
    point = TelemetryPoint(datetime(2024, 3, 1, tzinfo=timezone.utc), "fitbit", Metric.heart_rate, 61.125, "bpm")
    good = TelemetryStore(tmp_data_dir)
    good.save("prior", [point])
    before = good.path_for("prior").read_bytes()
    def fail(handle, data):
        handle.write(data[:20])
        raise OSError("disk full")
    broken = TelemetryStore(tmp_data_dir, writer=fail)
    with pytest.raises(User_Error) as exc:
        broken.save("failed", [point], ["heart_rate-2024-03-01.json"])
    assert exc.value.code == "SAVE_FAILED" and exc.value.file_name
    assert good.path_for("prior").read_bytes() == before
    assert list(good.telemetry_dir.iterdir()) == [good.path_for("prior")]


@pytest.mark.parametrize("change", ["naive", "nonfinite", "metric", "unit"])
def test_invalid_persisted_point_rejects_entire_import(tmp_data_dir, change):
    point = TelemetryPoint(datetime(2024, 3, 1, tzinfo=timezone.utc), "fitbit", Metric.heart_rate, 61, "bpm")
    store = TelemetryStore(tmp_data_dir)
    store.save("bad", [point, point])
    payload = json.loads(store.path_for("bad").read_bytes())
    row = payload["points"][1]
    if change == "naive":
        row["timestamp"] = "2024-03-01T00:00:00"
    elif change == "nonfinite":
        row["value"] = float("inf")
    elif change == "metric":
        row["metric"] = "unsupported"
    else:
        row["unit"] = "watts"
    store.path_for("bad").write_text(json.dumps(payload))
    with pytest.raises(User_Error) as exc:
        store.load("bad", ["bad.json"])
    assert exc.value.code == "PERSISTED_DATA_UNREADABLE" and exc.value.file_name == "bad.json"


def test_logs_exclude_values_timestamps_segments_contents_and_exception_locals(tmp_data_dir):
    log_path = configure_logging(tmp_data_dir)
    log = get_logger("test")
    secret = "sensitive-export-content-938271"
    timestamp = "2024-03-01T22:12:34-05:00"
    try:
        log.info("import.complete", file_name="export.csv", row_count=42,
                 telemetry_values=[71.98765], timestamps=[timestamp], stages=[secret], contents=secret)
        try:
            raise RuntimeError(secret + timestamp + "71.98765")
        except RuntimeError as error:
            log.exception("import.failed", error, file_name="export.csv")
        with pytest.raises(LogFieldError):
            get_logger("strict", strict=True).info("import.complete", value=71.98765)
    finally:
        shutdown_logging()
    text = log_path.read_text()
    assert "export.csv" in text and "42" in text
    assert all(value not in text for value in (secret, timestamp, "71.98765"))
    assert log_path.is_relative_to(tmp_data_dir)
