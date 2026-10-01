"""The four fixed voices: additive pad, sine pulse, filtered noise, soft blips."""
import random

import numpy as np

from backend.domain.sound import Note_Event
from backend.audio.envelopes import envelope
from backend.audio.scales import frequency
from backend.sonification.presets import PRESETS


def pulse_rate(value):
    return 0.5 + 1.5 * value


def schedule_notes(plan, key):
    rng = random.Random(f"{plan.seed}:pad")
    notes = []
    for span in plan.preset_plan:
        t = span.start_s + 0.3
        index = 0
        while t < span.end_s:
            preset = PRESETS[span.preset]
            duration = min(5.0, plan.target_duration_s - t)
            if duration >= 0.6 + 1.0:
                notes.append(Note_Event(t, duration, frequency(key, rng.randrange(5), 4), 0.028, "pad"))
            interval = (3, 17, 5, 21)[index % 4] if span.preset == "Awake" else preset.note_interval_s
            t += interval * (0.95 + 0.1 * rng.random())
            index += 1
    return tuple(notes)


def pad(times, parameters, preset, key, phases):
    bass = np.sin(2 * np.pi * frequency(key, 0, 2) * times + phases[0])
    upper = sum(np.sin(2 * np.pi * frequency(key, degree, 4) * times + phases[degree + 1]) / 3 for degree in (0, 2, 3))
    harmonics = np.sin(2 * np.pi * frequency(key, 0, 2) * 3 * times + phases[5])
    depth = preset.modulation_depth * (0.5 + parameters["modulation"])
    modulation = 1 - depth / 2 + depth / 2 * np.sin(2 * np.pi * preset.modulation_rate * times)
    return 0.12 * (preset.bass_gain * bass + preset.upper_gain * upper +
                   preset.harmonic_gain * parameters["brightness"] * harmonics) * modulation * (0.6 + 0.8 * parameters["intensity"])


def pulse(times, phase, key, rhythmic_density=0):
    # A smooth periodic pulse envelope, with no sharp gates or retrigger jumps.
    shape = np.maximum(0, np.sin(2 * np.pi * phase)) ** 6
    shape += 0.3 * rhythmic_density * np.maximum(0, np.sin(4 * np.pi * phase)) ** 6
    return 0.028 * shape * np.sin(2 * np.pi * frequency(key, 0, 2) * times)


def filtered_noise(times, knots, cutoff_hz):
    # Linear interpolation is a triangular low-pass reconstruction kernel.
    indices = times * (2 * cutoff_hz)
    left = np.minimum(indices.astype(np.int64), len(knots) - 2)
    fraction = indices - left
    return knots[left] * (1 - fraction) + knots[left + 1] * fraction


def texture(times, parameters, preset, noise, grain_phase):
    grain = 0.7 + 0.3 * np.sin(2 * np.pi * grain_phase) ** 2
    depth = preset.modulation_depth * (0.5 + parameters["modulation"])
    modulation = 1 - depth / 2 + depth / 2 * np.sin(2 * np.pi * preset.modulation_rate * times)
    return noise * preset.texture_gain * (0.3 + parameters["texture_density"]) * (0.6 + parameters["intensity"]) * modulation * grain


def transient(times, event, pitch, duration):
    return event.amplitude * 0.09 * envelope(times, event.onset_s, duration, 0.015, 0.12) * np.sin(2 * np.pi * pitch * times)


def note(times, event):
    return event.amplitude * envelope(times, event.onset_s, event.duration_s, 0.6, 1.0) * np.sin(2 * np.pi * event.pitch_hz * times)
