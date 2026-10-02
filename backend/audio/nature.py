"""Original unpitched nature synthesis; no recordings, loops or network access.

Independent seeded noise bands use absolute replay time. Rendering block size
cannot reset a noise stream, wave phase, envelope or crossfade at a boundary.
"""

from hashlib import sha256

import numpy as np

from backend.audio.envelopes import envelope, replay_fade
from backend.audio.renderer import BLOCK_FRAMES, SAMPLE_RATE, _gains
from backend.domain.sound import Mapping_Target

ACTIVITY = {"Deep": 0.65, "Light": 0.9, "Neutral": 0.85, "REM": 1.05, "Awake": 1.15}
NOISE_RATES = {
    "wind": 120,
    "air": 850,
    "surf": 1800,
    "foam": 4200,
    "rain": 8000,
    "leaves": 2600,
    "rumble": 38,
    "gust": 0.13,
    "spray": 1.7,
}


def _knots(seed, name, rate, duration):
    digest = sha256(f"{seed}:nature-v1:{name}".encode()).digest()
    rng = np.random.Generator(np.random.PCG64(int.from_bytes(digest[:16], "big")))
    return rng.uniform(-1, 1, int(np.ceil(duration * rate)) + 2)


def _noise(knots, rate, times):
    position = times * rate
    index = position.astype(np.int64)
    fraction = position - index
    smooth = fraction * fraction * (3 - 2 * fraction)
    return knots[index] + (knots[index + 1] - knots[index]) * smooth


def _curve(trajectory, times):
    if trajectory is None:
        return np.zeros_like(times)
    return np.interp(
        times,
        [b.t for b in trajectory.breakpoints],
        [b.value for b in trajectory.breakpoints],
    )


def _integral(trajectory, times):
    """Exact integral of a piecewise linear control, independent of block size."""
    points = trajectory.breakpoints
    if len(points) == 1:
        return points[0].value * times
    x = np.array([b.t for b in points])
    y = np.array([b.value for b in points])
    slopes = np.diff(y) / np.diff(x)
    areas = x[0] * y[0] + np.r_[0, np.cumsum(np.diff(x) * (y[:-1] + y[1:]) / 2)]
    index = np.clip(np.searchsorted(x, times, side="right") - 1, 0, len(x) - 2)
    delta = np.clip(times, x[0], x[-1]) - x[index]
    interior = areas[index] + y[index] * delta + slopes[index] * delta * delta / 2
    return np.where(
        times < x[0], times * y[0], interior + np.maximum(0, times - x[-1]) * y[-1]
    )


def render_nature(plan, *, block_frames=BLOCK_FRAMES):
    if type(block_frames) is not int or block_frames <= 0:
        raise ValueError("positive integer block size required")
    n = plan.target_duration_s * SAMPLE_RATE
    mix = np.empty((n, 2), dtype=np.float64)
    banks = {
        name: [
            _knots(plan.seed, f"{name}:{channel}", rate, plan.target_duration_s)
            for channel in ("shared", "left", "right")
        ]
        for name, rate in NOISE_RATES.items()
    }
    wave_offset = (
        int.from_bytes(sha256(f"{plan.seed}:surf-phase".encode()).digest()[:4], "big")
        / 2**32
    )
    for lo in range(0, n, block_frames):
        hi = min(n, lo + block_frames)
        times = np.arange(lo, hi, dtype=np.float64) / SAMPLE_RATE
        parameters = {p.value: _curve(t, times) for p, t in plan.trajectories.items()}
        variation = {
            k: _curve(plan.nature_controls.get(k), times)
            for k in ("temperature", "humidity", "pressure")
        }
        activity = sum(
            ACTIVITY[name] * gain
            for name, gain in _gains(plan.preset_plan, times).items()
        )
        # A wave-shaped amplitude envelope, never a pitched oscillator.
        phase = (
            wave_offset
            + 0.06 * times
            + 0.065 * _integral(plan.trajectories[Mapping_Target.pulse_rate], times)
        )
        wave = (0.5 - 0.5 * np.cos(2 * np.pi * phase)) ** 2
        gust = 0.5 + 0.5 * _noise(banks["gust"][0], NOISE_RATES["gust"], times)
        spray = 0.5 + 0.5 * _noise(banks["spray"][0], NOISE_RATES["spray"], times)
        wind_gain = (
            0.065 + 0.04 * parameters["intensity"] + 0.055 * variation["pressure"]
        ) * (0.7 + 0.5 * gust * parameters["modulation"])
        surf_gain = (0.085 + 0.025 * parameters["rhythmic_density"]) * (
            0.45 + 0.9 * wave
        )
        rain_gain = (
            0.005 + 0.065 * variation["humidity"] + 0.02 * parameters["texture_density"]
        ) * (0.65 + 0.5 * spray)
        leaf_gain = (
            0.005
            + 0.025 * parameters["transient_density"]
            + 0.035 * variation["temperature"]
        )
        rumble_gain = 0.006 + 0.035 * variation["pressure"]
        fade = replay_fade(times, (n - 1) / SAMPLE_RATE)
        for channel in range(2):
            noise = {
                name: 0.78 * _noise(knots[0], NOISE_RATES[name], times)
                + 0.22 * _noise(knots[channel + 1], NOISE_RATES[name], times)
                for name, knots in banks.items()
                if name not in ("gust", "spray")
            }
            voice = wind_gain * (
                noise["wind"] + (0.2 + 0.4 * parameters["brightness"]) * noise["air"]
            )
            voice += surf_gain * (noise["surf"] + (0.3 + 0.3 * wave) * noise["foam"])
            voice += rain_gain * noise["rain"] + leaf_gain * spray * noise["leaves"]
            voice += rumble_gain * noise["rumble"]
            voice *= 0.75 + 0.25 * activity
            for event in plan.transients:
                if event.onset_s < times[-1] and event.onset_s + 1.5 > times[0]:
                    voice += (
                        0.045
                        * event.amplitude
                        * envelope(times, event.onset_s, 1.5, 0.2, 0.6)
                        * noise["leaves"]
                    )
            for gesture in plan.gestures:
                if gesture.start_s < times[-1] and gesture.end_s > times[0]:
                    voice += (
                        0.025
                        * envelope(
                            times,
                            gesture.start_s,
                            gesture.end_s - gesture.start_s,
                            0.5,
                            0.5,
                        )
                        * noise["surf"]
                    )
            mix[lo:hi, channel] = voice * fade
    if not np.isfinite(mix).all():
        raise ValueError("nature synthesis produced non-finite audio")
    return mix
