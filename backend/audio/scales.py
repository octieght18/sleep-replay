"""One seeded pentatonic-major key for every pitched layer in a replay."""
import random

PENTATONIC_MAJOR = (0, 2, 4, 7, 9)


def replay_key(seed):
    return random.Random(f"{seed}:scale").randrange(12)


def frequency(key, degree=0, octave=3):
    midi = 12 * (octave + 1) + key + PENTATONIC_MAJOR[degree % 5]
    return 440.0 * 2 ** ((midi - 69) / 12)


def in_scale(pitch_hz, key):
    import math
    midi = round(69 + 12 * math.log2(pitch_hz / 440))
    return (midi - key) % 12 in PENTATONIC_MAJOR
