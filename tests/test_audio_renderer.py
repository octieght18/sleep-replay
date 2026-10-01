import numpy as np
import pytest
from hypothesis import given, settings, strategies as st

from backend.audio.envelopes import envelope, replay_fade
from backend.audio.layers import pulse_rate, schedule_notes
from backend.audio.loudness import quantize, scale_loudness
from backend.audio.renderer import render
from backend.audio.scales import in_scale, replay_key
from backend.audio.wav import wav_bytes
from backend.domain.errors import User_Error
from backend.domain.sound import SOUND_PARAMETERS, Preset_Span
from backend.sonification.presets import PRESETS
from sonification_helpers import assert_audio, constant_plan, read_wav


# Feature: sleep-replay-sonification, Property 4: Pulse rate bounds and monotonicity
@settings(max_examples=100)
@given(st.floats(0, 1), st.floats(0, 1))
def test_pulse_rate(a, b):
    a, b = sorted((a, b))
    assert pulse_rate(0) == 0.5 and pulse_rate(1) == 2
    assert 0.5 <= pulse_rate(a) <= pulse_rate(b) <= 2


# Feature: sleep-replay-sonification, Property 16: WAV frame count
@settings(max_examples=5, deadline=None)
@given(st.sampled_from((30, 120, 180, 300, 600)))
def test_frame_count(duration):
    plan = constant_plan(duration, "Deep")
    header, _ = read_wav(wav_bytes(quantize(scale_loudness(render(plan)))))
    assert header == (44100, 2, 2, duration * 44100)


# Feature: sleep-replay-sonification, Property 17: Audio output bounds
@settings(max_examples=5, deadline=None)
@given(st.sampled_from(tuple(PRESETS)), st.integers(0, 2**32 - 1),
       st.lists(st.floats(0, 1), min_size=7, max_size=7))
def test_output_bounds(preset, seed, values):
    plan = constant_plan(30, preset, seed, dict(zip(SOUND_PARAMETERS, values)))
    header, samples = read_wav(wav_bytes(quantize(scale_loudness(render(plan)))))
    assert_audio(header, samples, 30)


@pytest.fixture(scope="module")
def comparison_renders():
    measures = {}
    for name in PRESETS:
        plan = constant_plan(120, name)
        samples = render(plan)[5 * 44100:115 * 44100].mean(axis=1)
        spectrum = np.abs(np.fft.rfft(samples * np.hanning(len(samples))))
        frequencies = np.fft.rfftfreq(len(samples), 1 / 44100)
        centroid = np.sum(frequencies * spectrum) / np.sum(spectrum)
        low_share = np.sum(spectrum[frequencies < 200] ** 2) / np.sum(spectrum ** 2)
        onsets = np.array([n.onset_s for n in schedule_notes(plan, replay_key(plan.seed)) if 5 <= n.onset_s <= 115])
        measures[name] = (centroid, low_share, onsets)
    return measures


def test_deep_light_neutral_spectral_character(comparison_renders):
    deep, light, neutral = [comparison_renders[k] for k in ("Deep", "Light", "Neutral")]
    assert deep[0] <= 0.8 * light[0]
    assert deep[1] >= light[1] + 0.10
    assert deep[0] <= neutral[0] <= light[0]
    assert light[1] <= neutral[1] <= deep[1]
    assert PRESETS["Deep"].modulation_rate < PRESETS["Light"].modulation_rate
    assert PRESETS["Deep"].modulation_rate <= PRESETS["Neutral"].modulation_rate <= PRESETS["Light"].modulation_rate


def test_rem_and_awake_character(comparison_renders):
    assert all(PRESETS["REM"].modulation_depth >= preset.modulation_depth + 0.2 for name, preset in PRESETS.items() if name != "REM")
    awake, light = comparison_renders["Awake"][2], comparison_renders["Light"][2]
    assert len(awake) >= 3 and len(awake) <= 0.5 * len(light)
    assert np.std(np.diff(awake)) / np.mean(np.diff(awake)) >= 0.3


def test_all_scheduled_pitches_share_one_scale():
    for seed in (0, 1, 123456, 2**32 - 1):
        plan = constant_plan(30, "Awake", seed)
        key = replay_key(seed)
        assert all(in_scale(note.pitch_hz, key) for note in schedule_notes(plan, key))


def test_envelope_minima_and_fades():
    t = np.array([0, 0.0025, 0.005, 0.98, 0.99, 1])
    assert envelope(t, 0, 1).tolist() == pytest.approx([0, 0.5, 1, 1, 0.5, 0])
    for attack, release in ((0.001, 0.02), (0.005, 0.001)):
        with pytest.raises(ValueError):
            envelope(t, 0, 1, attack, release)
    assert replay_fade(np.array([0, 1, 2, 26, 27, 28, 29]), 29).tolist() == pytest.approx([0, 0.5, 1, 1, 2/3, 1/3, 0])


def test_nonfinite_and_silence_are_rejected():
    for samples in (np.full((30 * 44100, 2), np.nan), np.full((100, 2), np.inf), np.zeros((100, 2))):
        with pytest.raises(User_Error, match="RENDERING_FAILED"):
            scale_loudness(samples)


def test_pcm_rounding_and_peak_grid():
    data = np.array([[0.5 / 32768, -0.5 / 32768], [0.891, -0.891]])
    result = quantize(data)
    assert result[0].tolist() == [1, -1]
    assert np.max(np.abs(result.astype(float) / 32768)) <= 0.891
