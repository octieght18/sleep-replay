import json
import os
import subprocess
import sys
import zipfile
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter

import numpy as np
import pytest
from hypothesis import given, settings, strategies as st

from backend.api.pipeline import Pipeline
from backend.domain.errors import User_Error
from backend.domain.mapping import DEFAULT_MAPPING, METRIC_KEYS, mapping_from_dict
from backend.domain.session import SleepSession
from backend.domain.stages import Sleep_Stage
from backend.domain.telemetry import Metric
from backend.domain.timezones import FILE_TYPES
from backend.processing.features import extract
from backend.sonification.engine import build_render_plan
from backend.sonification.generation import generate_replay
from backend.sonification.manifest import manifest_dict, parse_manifest
from sonification_helpers import assert_audio, read_wav

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "sample_data"
CONFIG = replace(DEFAULT_MAPPING, target_duration=30)
OVERRIDES = {kind: "America/New_York" for kind in FILE_TYPES}


@pytest.fixture
def processed(tmp_path):
    with Pipeline(tmp_path / "data", display_timezone="America/New_York") as pipeline:
        pipeline.use_sample_data()
        yield pipeline, pipeline.process(mapping_config=CONFIG)


def generate(pipeline, result, config=CONFIG, **kwargs):
    return generate_replay(result.session, result.timeline, result.features, result.coarse_states, result.events,
        data_dir=pipeline.data_dir, metadata_store=pipeline.metadata_store, config=config, display_timezone="America/New_York",
        processing_report=result.report, input_fingerprint=result.input_fingerprint, **kwargs)


# Feature: sleep-replay-sonification, Property 18: Replay determinism and confluence
@settings(max_examples=5, deadline=None)
@given(st.integers(0, 2**32 - 2), st.permutations(tuple(p.name for p in sorted(SAMPLE.iterdir()))))
def test_generation_determinism_confluence(seed, order):
    outputs = []
    for paths, next_seed in ((order, seed), (tuple(reversed(order)), seed), (order, seed + 1)):
        with TemporaryDirectory() as d, Pipeline(d, display_timezone="America/New_York") as p:
            fitbit = [SAMPLE / name for name in paths if name != "sensorpush.csv"]
            requests = [("fitbit", fitbit, OVERRIDES), ("sensorpush", [SAMPLE / "sensorpush.csv"], OVERRIDES)]
            if paths[0] == "sensorpush.csv":
                requests.reverse()
            p.import_sources(requests)
            generation = p.generate(CONFIG, seed=next_seed)
            outputs.append((generation.wav_path.read_bytes(), generation.manifest_path.read_bytes()))
    assert outputs[0] == outputs[1]
    assert outputs[0][0] != outputs[2][0]
    first, third = [json.loads(outputs[i][1]) for i in (0, 2)]
    first["random_seed"] = third["random_seed"]
    first["mapping_config"]["random_seed"] = third["mapping_config"]["random_seed"]
    assert first == third


# Feature: sleep-replay-sonification, Property 20: Graceful degradation loudness
@settings(max_examples=5, deadline=None)
@given(st.sets(st.sampled_from(METRIC_KEYS)))
def test_degradation_loudness(unavailable):
    with TemporaryDirectory() as d, Pipeline(d, display_timezone="America/New_York") as p:
        p.use_sample_data()
        result = p.process(mapping_config=CONFIG)
        metric_keys = {Metric.hrv_rmssd: "hrv", Metric.steps: "movement"}
        telemetry = tuple(point for point in result.session.telemetry if metric_keys.get(point.metric, point.metric.value) not in unavailable)
        session = replace(result.session, telemetry=telemetry, session_hrv=None)
        timeline = replace(result.timeline,
            values={metric: ((None,) * result.timeline.sample_count if metric_keys.get(metric, metric.value) in unavailable else values)
                    for metric, values in result.timeline.values.items()},
            missing={metric: ((True,) * result.timeline.sample_count if metric_keys.get(metric, metric.value) in unavailable else values)
                     for metric, values in result.timeline.missing.items()})
        feature_series, states, report = extract(timeline, session, 30)
        degraded = replace(result, session=session, timeline=timeline, features=feature_series, coarse_states=tuple(states), report=report)
        replay = generate(p, degraded)
        header, samples = read_wav(replay.wav_path.read_bytes())
        assert_audio(header, samples, 30)
        assert set(replay.manifest.unavailable_metrics) == unavailable - {"movement"}


def test_end_to_end_default_and_stored_config(tmp_path):
    with Pipeline(tmp_path / "data", display_timezone="America/New_York") as p:
        p.use_sample_data()
        replay = p.generate()
        header, samples = read_wav(replay.wav_path.read_bytes())
        assert_audio(header, samples, 180)
        assert parse_manifest(replay.manifest_path.read_bytes()) == replay.manifest
        assert 1 <= len(replay.manifest.night_events) <= 12
        record = p.metadata_store.get_replay(replay.replay_id)
        assert record.created_at.utcoffset() is not None
        assert "created_at" not in manifest_dict(replay.manifest)
        assert mapping_from_dict(record.metadata["mapping_config"]) == DEFAULT_MAPPING
        assert (p.data_dir / record.metadata["manifest_file"]).exists()


def test_no_usable_data_and_invalid_seed_leave_no_records(processed):
    p, result = processed
    unusable = replace(result, session=replace(result.session, stages=(), telemetry=()))
    with pytest.raises(User_Error) as error:
        generate(p, unusable)
    assert error.value.code == "NO_USABLE_DATA"
    assert "Fitbit" in error.value.action and "SensorPush" in error.value.action
    for seed in (-1, True, "1", 2**32):
        with pytest.raises(User_Error, match="INVALID_RANDOM_SEED"):
            generate(p, result, seed=seed)
    assert not p.metadata_store.list_replays()
    assert not (p.data_dir / "replays").exists()


def test_render_failure_before_any_writes(processed, monkeypatch, tmp_path):
    from backend.sonification import generation
    p, result = processed
    output = tmp_path / "existing.wav"
    output.write_bytes(b"earlier replay")
    monkeypatch.setattr(generation, "render", lambda plan: np.full((30 * 44100, 2), np.nan))
    with pytest.raises(User_Error, match="RENDERING_FAILED"):
        generate(p, result, output=output)
    assert output.read_bytes() == b"earlier replay"
    assert not p.metadata_store.list_replays()
    assert not (p.data_dir / "replays").exists()


@pytest.mark.parametrize("failure", ["rename", "database"])
def test_write_failure_restores_files_and_records(processed, monkeypatch, tmp_path, failure):
    from backend.sonification import generation
    p, result = processed
    earlier = generate(p, result)
    output = tmp_path / "output.wav"
    output.write_bytes(b"original wav")
    output.with_suffix(".json").write_bytes(b"original json")
    original = generation.os.replace
    calls = 0
    def fail_replace(src, dst):
        nonlocal calls
        calls += 1
        if calls == 5:
            raise OSError("synthetic write failure")
        return original(src, dst)
    if failure == "rename":
        monkeypatch.setattr(generation.os, "replace", fail_replace)
    else:
        monkeypatch.setattr(p.metadata_store, "record_replay", lambda *a, **k: (_ for _ in ()).throw(User_Error("SAVE_FAILED", "Synthetic DB failure.", "Try again.")))
    with pytest.raises(User_Error, match="REPLAY_WRITE_FAILED") as error:
        generate(p, result, output=output)
    assert str(p.data_dir) in error.value.description
    assert output.read_bytes() == b"original wav" and output.with_suffix(".json").read_bytes() == b"original json"
    assert len(p.metadata_store.list_replays()) == 1
    assert earlier.wav_path.exists()
    assert len(list((p.data_dir / "replays").iterdir())) == 2
    assert not list(tmp_path.rglob(".replay-*"))


def test_fitbit_and_sensorpush_only_degradation(tmp_path):
    with Pipeline(tmp_path / "fitbit", display_timezone="America/New_York") as p:
        p.import_files("fitbit", sorted(x for x in SAMPLE.iterdir() if x.name != "sensorpush.csv"), OVERRIDES)
        replay = p.generate(CONFIG)
        assert "SensorPush environmental data is absent." in replay.manifest.warnings
        assert set(replay.manifest.unavailable_metrics) == {"temperature", "humidity", "pressure"}
    with Pipeline(tmp_path / "sensor", display_timezone="America/New_York") as p:
        with pytest.raises(User_Error, match="NO_SLEEP_SESSION"):
            p.import_files("sensorpush", [SAMPLE / "sensorpush.csv"], OVERRIDES)
        p.define_manual_range("2024-03-01T22:00", "2024-03-02T06:00")
        replay = p.generate(CONFIG)
        assert "Fitbit data is absent." in replay.manifest.warnings
        assert any("Neutral" in w for w in replay.manifest.warnings)
        assert replay.manifest.coarse_states == ((0, 30, "unknown"),)
        assert set(replay.manifest.unavailable_metrics) == {"heart_rate", "hrv", "movement"}


def test_cross_process_environment_path_and_history_independence(processed, tmp_path):
    p, result = processed
    first = generate(p, result)
    # Rendering other seeds beforehand cannot consume the next replay's PRNG.
    generate(p, result, seed=9)
    p.metadata_store.set_setting("display_units", "imperial")
    second = generate(p, result, output=tmp_path / "export" / "same.wav")
    assert first.wav_path.read_bytes() == second.wav_path.read_bytes()
    assert first.manifest_path.read_bytes() == second.manifest_path.read_bytes()
    output = tmp_path / "new-process.wav"
    env = dict(os.environ, TZ="Asia/Tokyo", LC_ALL="C", SLEEP_REPLAY_DATA_DIR=str(tmp_path / "fresh"))
    command = [sys.executable, "-m", "backend.api.cli", "generate", *map(str, sorted(SAMPLE.iterdir())),
        "--target-duration", "30", "--display-timezone", "America/New_York", "--fitbit-heart-rate-timezone", "America/New_York",
        "--fitbit-steps-timezone", "America/New_York", "--output", str(output)]
    child = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True)
    assert child.returncode == 0, child.stderr
    assert output.read_bytes() == first.wav_path.read_bytes()
    assert output.with_suffix(".json").read_bytes() == first.manifest_path.read_bytes()


def test_zip_member_order_confluence(tmp_path):
    replays = []
    for index, files in enumerate((sorted(SAMPLE.iterdir()), list(reversed(sorted(SAMPLE.iterdir()))))):
        archive = tmp_path / f"night-{index}.zip"
        with zipfile.ZipFile(archive, "w") as writer:
            for path in files:
                if path.name != "sensorpush.csv":
                    writer.writestr("export/" + path.name, path.read_bytes())
        with Pipeline(tmp_path / f"data-{index}", display_timezone="America/New_York") as p:
            p.import_sources([("fitbit", [archive], OVERRIDES), ("sensorpush", [SAMPLE / "sensorpush.csv"], OVERRIDES)])
            replay = p.generate(CONFIG)
            replays.append((replay.wav_path.read_bytes(), replay.manifest_path.read_bytes()))
    assert replays[0] == replays[1]


@pytest.mark.reference_machine
@pytest.mark.parametrize("duration,limit", [(30, 60), (120, 60), (180, 60), (300, 180), (600, 180)])
def test_generation_performance(tmp_path, duration, limit):
    with Pipeline(tmp_path / "data", display_timezone="America/New_York") as p:
        p.use_sample_data()
        start = perf_counter()
        replay = p.generate(replace(DEFAULT_MAPPING, target_duration=duration))
        assert replay.wav_path.exists() and replay.manifest_path.exists()
        assert perf_counter() - start < limit
