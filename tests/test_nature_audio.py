"""Nature rendering: audible data response, continuity, determinism and rights."""

import json
from dataclasses import replace

import numpy as np
import pytest
from hypothesis import given, settings, strategies as st

from backend.audio.loudness import quantize, scale_loudness
from backend.audio.nature import render_nature
from backend.audio.renderer import SAMPLE_RATE, render
from backend.audio.wav import wav_bytes
from backend.domain.audio_provenance import nature_provenance
from backend.domain.errors import User_Error
from backend.domain.mapping import DEFAULT_MAPPING, TARGET_DURATIONS
from backend.domain.sound import (
    Breakpoint,
    Mapping_Target,
    Onset_Gesture,
    Parameter_Trajectory,
    Preset_Span,
    Transient_Event,
)
from backend.domain.stages import Sleep_Stage
from backend.sonification.config_parser import parse_config
from backend.sonification.engine import build_render_plan
from backend.sonification.manifest import (
    manifest_dict,
    parse_manifest,
    serialize_manifest,
)
from backend.sonification.nature import VARIATION_RATE_LIMIT, nature_controls
from sonification_helpers import assert_audio, constant_plan, features, read_wav
from tests.test_replay_manifest import manifest

NATURE = replace(DEFAULT_MAPPING, sound_style="nature", target_duration=30)


def fixed(value):
    return Parameter_Trajectory((Breakpoint(0, value), Breakpoint(30, value)))


@pytest.mark.parametrize("value", [None, True, 0, [], {}, "rain", "Nature"])
def test_invalid_style(value):
    with pytest.raises(User_Error, match="sound_style"):
        parse_config(json.dumps({"sound_style": value}))


@settings(max_examples=100)
@given(st.floats(-20, 35), st.floats(0, 10), st.floats(0, 1))
def test_variation_bounded_rate_limited_and_offset_independent(
    baseline, departure, sensitivity
):
    config = replace(
        NATURE,
        metrics={
            **NATURE.metrics,
            "temperature": replace(
                NATURE.metrics["temperature"], sensitivity=sensitivity
            ),
        },
    )
    values = [baseline] * 30 + [baseline + departure] * 10 + [None] * 20
    controls = nature_controls(features(values, key="temperature"), config, 30)
    curve = controls["temperature"]
    for a, b in zip(curve.breakpoints, curve.breakpoints[1:]):
        assert 0 <= b.value <= sensitivity
        assert abs(b.value - a.value) <= VARIATION_RATE_LIMIT * (b.t - a.t) + 1e-12
    assert curve.value_at(10) == 0
    assert curve.value_at(26) == 0  # Missing data returns slowly to calm.
    shifted = [None if v is None else v + 40 for v in values]
    other = nature_controls(features(shifted, key="temperature"), config, 30)[
        "temperature"
    ]
    assert [b.value for b in curve.breakpoints] == pytest.approx(
        [b.value for b in other.breakpoints], abs=1e-12
    )


@pytest.mark.parametrize(
    "key,baseline,scale",
    [("temperature", 20, 1), ("humidity", 45, 5), ("pressure", 1013, 1)],
)
def test_stronger_variation_and_disabled_or_missing_environment(key, baseline, scale):
    calm = features([baseline] * 60, key=key)
    slight = features(
        [baseline] * 30 + [baseline + scale / 4] * 10 + [baseline] * 20, key=key
    )
    strong = features(
        [baseline] * 30 + [baseline + scale] * 10 + [baseline] * 20, key=key
    )
    peaks = [
        max(p.value for p in nature_controls(f, NATURE, 30)[key].breakpoints)
        for f in (calm, slight, strong)
    ]
    assert peaks[0] == 0 < peaks[1] < peaks[2]
    for mapping in (
        replace(NATURE.metrics[key], sensitivity=0),
        replace(NATURE.metrics[key], target=Mapping_Target.none),
    ):
        config = replace(NATURE, metrics={**NATURE.metrics, key: mapping})
        assert all(
            b.value == 0 for b in nature_controls(strong, config, 30)[key].breakpoints
        )
    for f in (
        replace(strong, unavailable_metrics=(key,)),
        features([None] * 60, key=key),
    ):
        assert all(
            b.value == 0 for b in nature_controls(f, NATURE, 30)[key].breakpoints
        )


@pytest.mark.parametrize("duration", TARGET_DURATIONS)
def test_all_durations_steady_audible_and_within_audio_limits(duration):
    plan = replace(constant_plan(duration=duration), sound_style="nature")
    # scale_loudness checks every interior second and every sample boundary.
    samples = scale_loudness(render(plan))
    assert samples.shape == (duration * SAMPLE_RATE, 2)
    assert np.max(np.abs(samples[[0, -1]])) <= 0.001
    if duration == 30:
        assert_audio(*read_wav(wav_bytes(quantize(samples))), duration)


def test_no_block_seams_with_state_changes_events_and_data_gaps():
    f = features([20] * 20 + [23] * 20 + [None] * 20, key="temperature")
    plan = build_render_plan(
        f, (Sleep_Stage.deep,) * 20 + (Sleep_Stage.rem,) * 40, (), NATURE, 30, 55
    )
    plan = replace(
        plan,
        transients=(Transient_Event(10.73, 0.9, "movement"),),
        gestures=(Onset_Gesture("awakening", 19.7, 21.7),),
    )
    reference = render_nature(plan)
    assert np.array_equal(reference, render_nature(plan, block_frames=31741))
    assert_audio(*read_wav(wav_bytes(quantize(scale_loudness(reference)))), 30)
    with pytest.raises(TypeError):
        plan.nature_controls["temperature"] = fixed(1)


def test_seed_determinism_and_unpitched_nature_spectrum():
    plan = replace(constant_plan(), sound_style="nature")
    first = render(plan)
    assert np.array_equal(first, render(plan))
    assert not np.array_equal(first, render(replace(plan, seed=plan.seed + 1)))
    # No dominant musical carrier: energy is spread across noise frequencies.
    segment = first[5 * SAMPLE_RATE : 15 * SAMPLE_RATE, 0]
    power = abs(np.fft.rfft(segment * np.hanning(len(segment)))) ** 2
    power = power[np.fft.rfftfreq(len(segment), 1 / SAMPLE_RATE) > 30]
    assert np.max(power) / np.sum(power) < 0.01


@pytest.mark.parametrize("key", ["temperature", "humidity", "pressure"])
def test_environmental_layers_change_audio_and_frequency_balance(key):
    plan = replace(constant_plan(), sound_style="nature")
    baseline = render(plan)
    changed = render(replace(plan, nature_controls={key: fixed(1)}))
    assert np.sqrt(np.mean((changed - baseline) ** 2)) > 0.002
    frequencies = np.fft.rfftfreq(5 * SAMPLE_RATE, 1 / SAMPLE_RATE)
    spectra = [
        abs(np.fft.rfft(x[5 * SAMPLE_RATE : 10 * SAMPLE_RATE, 0])) ** 2
        for x in (baseline, changed)
    ]
    band = frequencies < 50 if key == "pressure" else frequencies > 1000
    assert spectra[1][band].sum() > spectra[0][band].sum() * 1.1


def test_all_neutral_and_stage_presets_remain_continuous():
    plan = replace(
        constant_plan(),
        sound_style="nature",
        preset_plan=tuple(
            Preset_Span(i * 6, (i + 1) * 6, name, 0 if i == 0 else 2)
            for i, name in enumerate(("Deep", "Light", "REM", "Awake", "Neutral"))
        ),
    )
    assert_audio(*read_wav(wav_bytes(quantize(scale_loudness(render(plan))))), 30)


def test_legacy_music_and_nature_manifest_license_round_trip():
    music = manifest()
    assert "sound_provenance" not in manifest_dict(music)
    assert parse_manifest(serialize_manifest(music)) == music
    nature = replace(music, mapping_config=NATURE, version="0.3.0")
    document = manifest_dict(nature)
    assert document["sound_provenance"] == nature_provenance()
    assert parse_manifest(serialize_manifest(nature)) == nature
    for replacement in (None, {**nature_provenance(), "license": "unknown"}):
        document["sound_provenance"] = replacement
        with pytest.raises(ValueError, match="sound_provenance"):
            parse_manifest(json.dumps(document))


def test_nature_generates_offline_and_survives_missing_environment(
    tmp_path, monkeypatch
):
    import socket
    from backend.api.pipeline import Pipeline
    from backend.domain.telemetry import Metric

    def blocked(*args, **kwargs):
        raise AssertionError("Nature must not download or fetch sounds")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    with Pipeline(tmp_path, display_timezone="America/New_York") as pipeline:
        pipeline.import_files("fitbit", ["sample_data/sleep-2024-03-01.json"])
        replay = pipeline.generate(NATURE)
        assert {"temperature", "humidity", "pressure"} <= set(
            replay.manifest.unavailable_metrics
        )
        assert all(
            all(v is None for v in row) for row in replay.manifest.environmental_windows
        )
        assert not any(
            p.metric is Metric.temperature for p in pipeline.selected_session.telemetry
        )
        assert_audio(*read_wav(replay.wav_path.read_bytes()), 30)
        assert parse_manifest(replay.manifest_path.read_bytes()) == replay.manifest
