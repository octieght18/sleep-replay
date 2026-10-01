"""Canonical 44.1 kHz, stereo, 16-bit PCM WAV encoding."""
import io
import wave

from backend.audio.renderer import SAMPLE_RATE


def wav_bytes(pcm):
    if pcm.ndim != 2 or pcm.shape[1] != 2 or pcm.dtype.str != "<i2":
        raise ValueError("WAV input must be little-endian int16 stereo")
    output = io.BytesIO()
    with wave.open(output, "wb") as writer:
        writer.setnchannels(2)
        writer.setsampwidth(2)
        writer.setframerate(SAMPLE_RATE)
        writer.writeframes(pcm.tobytes(order="C"))
    return output.getvalue()
