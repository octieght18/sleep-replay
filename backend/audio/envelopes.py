"""Continuous attack/release envelopes, including global endpoint fades."""
import numpy as np


def envelope(times, start, duration, attack=0.005, release=0.02):
    if attack < 0.005 or release < 0.02 or duration < attack + release:
        raise ValueError("envelope requires attack >= 5 ms, release >= 20 ms, and sufficient duration")
    relative = times - start
    return np.maximum(0.0, np.minimum(1.0, np.minimum(relative / attack, (duration - relative) / release)))


def replay_fade(times, last_sample_time):
    return np.maximum(0, np.minimum(1, np.minimum(times / 2.0, (last_sample_time - times) / 3.0)))
