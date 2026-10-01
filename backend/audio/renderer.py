"""Block-wise float64 stereo synthesis from the render plan alone."""
from __future__ import annotations

import random
import numpy as np

from backend.audio import layers
from backend.audio.envelopes import envelope, replay_fade
from backend.audio.scales import frequency, replay_key
from backend.domain.errors import User_Error
from backend.domain.sound import SOUND_PARAMETERS
from backend.sonification.presets import PRESETS

SAMPLE_RATE = 44100
BLOCK_FRAMES = SAMPLE_RATE


def _gains(spans, times):
    gains = {}
    for i, span in enumerate(spans):
        incoming = span.crossfade_in_s
        outgoing = spans[i + 1].crossfade_in_s if i + 1 < len(spans) else 0
        if times[-1] < span.start_s - incoming / 2 or times[0] > span.end_s + outgoing / 2:
            continue
        start = np.clip((times - span.start_s + incoming / 2) / incoming, 0, 1) if incoming else (times >= span.start_s).astype(float)
        end = np.clip((span.end_s + outgoing / 2 - times) / outgoing, 0, 1) if outgoing else (times < span.end_s).astype(float)
        gain = np.minimum(start, end)
        gains[span.preset] = gains.get(span.preset, 0) + gain
    return gains


def render(plan):
    n = plan.target_duration_s * SAMPLE_RATE
    mix = np.empty((n, 2), dtype=np.float64)
    key = replay_key(plan.seed)
    pad_rng = random.Random(f"{plan.seed}:pad")
    pulse_rng = random.Random(f"{plan.seed}:pulse")
    texture_rng = random.Random(f"{plan.seed}:texture")
    transient_rng = random.Random(f"{plan.seed}:transient")
    phases = [pad_rng.uniform(0, 2 * np.pi) for _ in range(6)]
    pulse_phase = pulse_rng.random()
    grain_phase = texture_rng.random()
    # Small low-rate noise buffers; generated independently of block evaluation.
    noise_knots = {name: np.fromiter((texture_rng.uniform(-1, 1) for _ in range(
        int(plan.target_duration_s * 2 * p.texture_cutoff_hz) + 2)), dtype=np.float64)
        for name, p in PRESETS.items()}
    notes = (*layers.schedule_notes(plan, key), *plan.notes)
    note_presets = [(note, next((s.preset for s in plan.preset_plan if s.start_s <= note.onset_s < s.end_s), "Neutral")) for note in notes]
    transients = [(e, frequency(key, transient_rng.randrange(5), 4), transient_rng.uniform(0.2, 0.55))
                  for e in plan.transients]
    arrays = {p: (np.array([b.t for b in plan.trajectories[p].breakpoints]),
                  np.array([b.value for b in plan.trajectories[p].breakpoints])) for p in SOUND_PARAMETERS}
    for lo in range(0, n, BLOCK_FRAMES):
        hi = min(n, lo + BLOCK_FRAMES)
        times = np.arange(lo, hi, dtype=np.float64) / SAMPLE_RATE
        parameters = {p.value: np.interp(times, *arrays[p]) for p in SOUND_PARAMETERS}
        phase_steps = layers.pulse_rate(parameters["pulse_rate"]) / SAMPLE_RATE
        phase = pulse_phase + np.cumsum(phase_steps)
        pulse_phase = phase[-1]
        grains = grain_phase + np.cumsum((0.15 + 0.8 * parameters["transient_density"]) / SAMPLE_RATE)
        grain_phase = grains[-1]
        left = layers.pulse(times, phase, key, parameters["rhythmic_density"])
        right = left.copy()
        gains = _gains(plan.preset_plan, times)
        for name, gain in gains.items():
            preset = PRESETS[name]
            pad = layers.pad(times, parameters, preset, key, phases)
            noise = layers.filtered_noise(times, noise_knots[name], preset.texture_cutoff_hz)
            texture = layers.texture(times, parameters, preset, noise, grains)
            left += gain * (pad + texture)
            right += gain * (pad * 0.98 + texture * 0.85)
        for note, preset_name in note_presets:
            if note.onset_s < times[-1] and note.onset_s + note.duration_s > times[0]:
                voice = layers.note(times, note) * gains.get(preset_name, 0)
                left += voice
                right += voice * 0.94
        for event, pitch, duration in transients:
            if event.onset_s < times[-1] and event.onset_s + duration > times[0]:
                voice = layers.transient(times, event, pitch, duration)
                left += voice
                right += voice * 0.9
        for gesture in plan.gestures:
            if gesture.start_s < times[-1] and gesture.end_s > times[0]:
                swell = envelope(times, gesture.start_s, gesture.end_s - gesture.start_s, 0.5, 0.5)
                pitch = frequency(key, 0 if gesture.event_type == "sleep_onset" else 3, 3)
                voice = 0.035 * swell * np.sin(2 * np.pi * pitch * times)
                progress = np.clip((times - gesture.start_s) / (gesture.end_s - gesture.start_s), 0, 1)
                upper_gain = (1 - progress) if gesture.event_type == "sleep_onset" else progress
                voice += 0.02 * swell * upper_gain * np.sin(2 * np.pi * pitch * 2 * times)
                left += voice
                right += voice
        fade = replay_fade(times, (n - 1) / SAMPLE_RATE)
        mix[lo:hi, 0], mix[lo:hi, 1] = left * fade, right * fade
    if not np.isfinite(mix).all():
        raise User_Error("RENDERING_FAILED", "The rendered audio contains non-finite samples.", "Try another mapping configuration or report this failure.")
    return mix
