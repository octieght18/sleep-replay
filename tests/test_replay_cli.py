import json
import os
import shlex
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from backend.api.cli import main
from backend.api.pipeline import Pipeline
from backend.domain.mapping import DEFAULT_MAPPING
from backend.persistence.metadata_store import MetadataStore
from backend.sonification.config_printer import print_config
from backend.sonification.manifest import parse_manifest

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "sample_data"


def arguments(output, duration="30"):
    return ["generate", *map(str, sorted(SAMPLE.iterdir())), "--target-duration", duration,
        "--display-timezone", "America/New_York", "--fitbit-heart-rate-timezone", "America/New_York",
        "--fitbit-steps-timezone", "America/New_York", "--output", str(output)]


def test_sample_data_defaults_byte_identity_and_directory_behavior(tmp_path, capsys):
    out = tmp_path / "synthetic"
    out.mkdir()
    (out / "unrelated.txt").write_text("keep me")
    (out / "sensorpush.csv").write_text("replace me")
    assert main(["sample-data", "--out", str(out)]) == 0
    captured = capsys.readouterr()
    assert not captured.err
    for original in SAMPLE.iterdir():
        assert (out / original.name).read_bytes() == original.read_bytes()
        assert str(out / original.name) in captured.out
    assert (out / "unrelated.txt").read_text() == "keep me"


@pytest.mark.parametrize("extra,code", [
    (["--target-duration", "60"], "INVALID_TARGET_DURATION"), (["--seed", "-1"], "INVALID_RANDOM_SEED"),
    (["--seed", "4294967296"], "INVALID_RANDOM_SEED"), (["--seed", "3.5"], "INVALID_RANDOM_SEED"),
    (["--manual-start", "2024-03-01T22:00"], "INVALID_MANUAL_RANGE"),
    (["--manual-end", "2024-03-02T06:00"], "INVALID_MANUAL_RANGE"),
    (["--session-date", "2024-03-02", "--manual-start", "2024-03-01T22:00", "--manual-end", "2024-03-02T06:00"], "INVALID_MANUAL_RANGE"),
    (["--session-date", "wrong"], "NO_SLEEP_SESSION"), (["--display-timezone", "bogus"], "INVALID_TIMEZONE"),
    (["--source-timezone", "bad=UTC"], "INVALID_TIMEZONE"), (["--source-timezone", "sensorpush=bad"], "INVALID_TIMEZONE"),
    (["--manual-start", "2024-03-02T06:00", "--manual-end", "2024-03-01T22:00"], "INVALID_MANUAL_RANGE"),
])
def test_argument_errors_preserve_existing_outputs(tmp_path, capsys, extra, code):
    output = tmp_path / "replay.wav"
    output.write_bytes(b"earlier WAV")
    output.with_suffix(".json").write_bytes(b"earlier manifest")
    assert main([*arguments(output), *extra]) != 0
    captured = capsys.readouterr()
    assert captured.out == "" and code in captured.err
    assert output.read_bytes() == b"earlier WAV"
    assert output.with_suffix(".json").read_bytes() == b"earlier manifest"


@pytest.mark.parametrize("args", [[], ["generate"], ["sample-data"], ["generate", "missing.json"], ["unknown"]])
def test_missing_arguments_and_inputs_are_structured(args, capsys):
    assert main(args) != 0
    captured = capsys.readouterr()
    assert captured.out == "" and "[SOURCE_LOAD_FAILED]" in captured.err
    assert "Traceback" not in captured.err


def test_cli_precedence_success_contract_and_persisted_settings_ignored(tmp_path, capsys, tmp_data_dir):
    config = tmp_path / "config.yaml"
    config.write_text(print_config(replace(DEFAULT_MAPPING, target_duration=120, random_seed=999)))
    with MetadataStore(tmp_data_dir) as store:
        store.set_settings({"target_duration": 600, "random_seed": 777})
    output = tmp_path / "result.wav"
    assert main([*arguments(output), "--config", str(config), "--seed", "42", "--session-date", "2024-03-02"]) == 0
    captured = capsys.readouterr()
    assert not captured.err
    assert str(output) in captured.out and str(output.with_suffix(".json")) in captured.out
    assert "2024-03-01T22:00:00-05:00" in captured.out and "960.0" in captured.out and "Night events: 12" in captured.out
    replay = parse_manifest(output.with_suffix(".json").read_bytes())
    assert replay.target_duration_s == 30 and replay.random_seed == 42
    assert replay.mapping_config.random_seed == 42
    assert all("Warning: " + w in captured.out for w in replay.warnings)
    with MetadataStore(tmp_data_dir) as store:
        assert store.get_settings() == {"target_duration": 600, "random_seed": 777}
        assert len(store.list_replays()) == 1
    assert not list(tmp_data_dir.glob(".cli-*"))


def test_config_file_precedence_when_cli_omits_values(tmp_path, capsys):
    config = tmp_path / "config.yaml"
    config.write_text(print_config(replace(DEFAULT_MAPPING, target_duration=30, random_seed=15)))
    output = tmp_path / "replay.wav"
    args = arguments(output)
    del args[args.index("--target-duration"):args.index("--target-duration") + 2]
    assert main([*args, "--config", str(config)]) == 0
    replay = parse_manifest(output.with_suffix(".json").read_bytes())
    assert replay.target_duration_s == 30 and replay.random_seed == 15


def test_no_candidate_date_lists_dates(tmp_path, capsys):
    output = tmp_path / "replay.wav"
    assert main([*arguments(output), "--session-date", "2024-03-03"]) != 0
    captured = capsys.readouterr()
    assert "NO_SLEEP_SESSION" in captured.err and "2024-03-02" in captured.err
    assert not output.exists() and not output.with_suffix(".json").exists()


def test_manual_sensorpush_only(tmp_path, capsys):
    output = tmp_path / "manual.wav"
    assert main(["generate", str(SAMPLE / "sensorpush.csv"), "--target-duration", "30",
        "--display-timezone", "America/New_York", "--manual-start", "2024-03-01T22:00",
        "--manual-end", "2024-03-02T06:00", "--output", str(output)]) == 0
    replay = parse_manifest(output.with_suffix(".json").read_bytes())
    assert replay.coarse_states == ((0, 30, "unknown"),)
    assert "Fitbit data is absent." in replay.warnings


def test_bad_config_and_unwritable_output_are_actionable(tmp_path, capsys):
    config = tmp_path / "config.yaml"
    config.write_text("heart_rate: {sensitivity: 2}")
    output = tmp_path / "output.wav"
    assert main([*arguments(output), "--config", str(config)]) != 0
    assert "INVALID_MAPPING_CONFIG" in capsys.readouterr().err
    assert not output.exists()
    obstacle = tmp_path / "not-a-directory"
    obstacle.write_text("keep")
    output = obstacle / "result.wav"
    assert main(arguments(output)) != 0
    error = capsys.readouterr().err
    assert "REPLAY_WRITE_FAILED" in error and "writable" in error
    assert obstacle.read_text() == "keep"


def test_internal_failure_has_reference_without_trace_or_payload(monkeypatch, capsys, tmp_path):
    from backend.api import cli
    def fail(args):
        raise RuntimeError("sensitive input content")
    monkeypatch.setattr(cli, "_generate", fail)
    assert main(arguments(tmp_path / "result.wav")) == 1
    error = capsys.readouterr().err
    assert "reference" in error and "RENDERING_FAILED" in error
    assert "sensitive" not in error and "Traceback" not in error


def test_config_smoothing_is_applied_by_pipeline(tmp_path):
    with Pipeline(tmp_path / "data", display_timezone="America/New_York") as p:
        p.use_sample_data()
        config = replace(DEFAULT_MAPPING, target_duration=30)
        first = p.process(mapping_config=config)
        metrics = dict(config.metrics, heart_rate=replace(config.metrics["heart_rate"], smoothing_minutes=60))
        second = p.process(mapping_config=replace(config, metrics=metrics))
        from backend.domain.telemetry import Metric
        assert any(a.per_metric[Metric.heart_rate].smoothed != b.per_metric[Metric.heart_rate].smoothed
                   for a, b in zip(first.features.windows, second.features.windows))


def test_documented_example_command_regenerates_bytes(tmp_path):
    output = tmp_path / "example_replay.wav"
    env = dict(os.environ, SLEEP_REPLAY_DATA_DIR=str(tmp_path / "data"))
    # Execute the actual documented command, changing only Python/output paths.
    line = next(line for line in (ROOT / "README.md").read_text().splitlines()
                if line.startswith("python -m backend.api.cli generate "))
    command = shlex.split(line)
    command[0] = sys.executable
    command[command.index("--output") + 1] = str(output)
    completed = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr
    assert output.read_bytes() == (ROOT / "examples" / "example_replay.wav").read_bytes()
    assert output.with_suffix(".json").read_bytes() == (ROOT / "examples" / "example_replay.json").read_bytes()
