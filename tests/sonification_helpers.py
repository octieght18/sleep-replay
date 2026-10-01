"""Synthetic feature plans and WAV readers for sonification tests."""
import io
import wave
from datetime import datetime, timedelta, timezone

import numpy as np

from backend.domain.features import Feature_Series, Feature_Window, Metric_Window_Features
from backend.domain.mapping import FEATURE_METRICS
from backend.domain.sound import Breakpoint, Parameter_Trajectory, Preset_Span, Render_Plan, SOUND_PARAMETERS
from backend.domain.stages import Sleep_Stage


def features(values, *, key="heart_rate", movement=0.0, state=Sleep_Stage.light, window_s=0.5, ratio=960):
    origin = datetime(2024, 3, 2, tzinfo=timezone.utc)
    windows = []
    for i, value in enumerate(values):
        present = value is not None
        m = Metric_Window_Features(value, value, value, 0 if present else None, 0 if present else 1, value)
        per_metric = {FEATURE_METRICS[key]: m} if key != "movement" else {}
        intensity = value if key == "movement" and present else movement
        windows.append(Feature_Window(origin + timedelta(seconds=i * window_s * ratio),
            origin + timedelta(seconds=(i + 1) * window_s * ratio), per_metric, {state: 1}, state, intensity))
    return Feature_Series(tuple(windows), ratio)


def constant_plan(duration=30, preset="Neutral", seed=20240301, values=None):
    return Render_Plan(duration, {p: Parameter_Trajectory((Breakpoint(0, (values or {}).get(p, 0.5)),
        Breakpoint(duration, (values or {}).get(p, 0.5)))) for p in SOUND_PARAMETERS}, (), (),
        (Preset_Span(0, duration, preset),), seed)


def read_wav(data):
    with wave.open(io.BytesIO(data), "rb") as reader:
        header = (reader.getframerate(), reader.getsampwidth(), reader.getnchannels(), reader.getnframes())
        samples = np.frombuffer(reader.readframes(reader.getnframes()), dtype="<i2").reshape(-1, 2).astype(float) / 32768
    return header, samples


def assert_audio(header, samples, duration):
    assert header == (44100, 2, 2, duration * 44100)
    assert np.isfinite(samples).all()
    assert np.max(np.abs(samples)) <= 0.891
    assert -28 <= 20 * np.log10(np.sqrt(np.mean(samples**2))) <= -16
    assert np.max(np.abs(np.diff(samples, axis=0))) < 0.3
    assert np.max(np.abs(samples[[0, -1]])) <= 0.001
    for t in range(2, duration - 3):
        assert np.sqrt(np.mean(samples[t * 44100:(t + 1) * 44100]**2)) >= 10 ** (-50 / 20)
