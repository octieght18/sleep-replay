"""One global gain followed by fixed round-half-away-from-zero PCM quantization."""
import math
import numpy as np

from backend.domain.errors import User_Error
from backend.audio.renderer import SAMPLE_RATE

PEAK_LIMIT = 0.891
TARGET_RMS = 10 ** (-22 / 20)


def rms(samples):
    # Chunked reductions avoid a second full-size audio buffer for 10 min renders.
    squared = sum(float(np.sum(block * block)) for block in np.array_split(samples, max(1, len(samples) // SAMPLE_RATE)))
    return math.sqrt(squared / samples.size)


def scale_loudness(samples):
    if samples.ndim != 2 or samples.shape[1] != 2 or not np.isfinite(samples).all():
        raise _failure("Audio samples must be finite stereo values.")
    level = rms(samples)
    peak = max(float(np.max(np.abs(samples[lo:lo + SAMPLE_RATE]))) for lo in range(0, len(samples), SAMPLE_RATE))
    if not level or not peak:
        raise _failure("The renderer produced silence.")
    gain = min(TARGET_RMS / level, PEAK_LIMIT / peak)
    samples *= gain
    level = rms(samples)
    if not 10 ** (-28 / 20) <= level <= 10 ** (-16 / 20):
        raise _failure("Audio loudness is outside the replay limits.")
    for start in range(2, len(samples) // SAMPLE_RATE - 3):
        if rms(samples[start * SAMPLE_RATE:(start + 1) * SAMPLE_RATE]) < 10 ** (-50 / 20):
            raise _failure("An interior audio window is too quiet.")
    for lo in range(0, len(samples) - 1, SAMPLE_RATE):
        if np.max(np.abs(np.diff(samples[lo:min(len(samples), lo + SAMPLE_RATE + 1)], axis=0))) >= 0.3:
            raise _failure("Audio contains an abrupt sample discontinuity.")
    if np.max(np.abs(samples[[0, -1]])) > 0.001:
        raise _failure("Audio endpoint fades failed.")
    return samples


def quantize(samples):
    if not np.isfinite(samples).all():
        raise _failure("Audio contains non-finite samples.")
    # The int16 grid may round above the peak ceiling; clamp to the inward grid.
    ceiling = math.floor(PEAK_LIMIT * 32768)
    result = np.empty(samples.shape, dtype="<i2")
    for lo in range(0, len(samples), SAMPLE_RATE):
        scaled = np.clip(samples[lo:lo + SAMPLE_RATE], -PEAK_LIMIT, PEAK_LIMIT) * 32768
        result[lo:lo + SAMPLE_RATE] = np.clip(np.copysign(np.floor(np.abs(scaled) + 0.5), scaled), -ceiling, ceiling).astype("<i2")
    return result


def _failure(description):
    return User_Error("RENDERING_FAILED", description, "Try another mapping configuration or report this failure.")
