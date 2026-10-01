"""Five sound characters and boundary-centered equal-power-independent crossfades."""
from dataclasses import dataclass

from backend.domain.sound import Preset_Span
from backend.sonification.contributions import window_spans

STAGE_CROSSFADE_DURATION_S = 2.0
RESTLESS_TEXTURE_BOOST = 0.25
STATE_PRESETS = dict(awake="Awake", light="Light", asleep="Light", restless="Light", deep="Deep", rem="REM", unknown="Neutral")


@dataclass(frozen=True)
class Soundscape_Preset:
    bass_gain: float
    upper_gain: float
    harmonic_gain: float
    texture_gain: float
    texture_cutoff_hz: float
    modulation_rate: float
    modulation_depth: float
    note_interval_s: float


PRESETS = {
    "Deep": Soundscape_Preset(1.3, 0.02, 0.0, 0.012, 120, 0.025, 0.08, 5),
    "Light": Soundscape_Preset(0.35, 0.8, 0.18, 0.04, 900, 0.07, 0.12, 3),
    "Neutral": Soundscape_Preset(0.82, 0.4, 0.08, 0.025, 400, 0.045, 0.10, 4),
    "REM": Soundscape_Preset(0.6, 0.5, 0.12, 0.045, 650, 0.11, 0.55, 3),
    "Awake": Soundscape_Preset(0.5, 0.55, 0.14, 0.025, 700, 0.06, 0.10, 10),
}


def preset_plan(features, states):
    spans = []
    for (start, end), state in zip(window_spans(features), states):
        preset = STATE_PRESETS[state.value]
        if spans and spans[-1].preset == preset:
            previous = spans.pop()
            spans.append(Preset_Span(previous.start_s, end, preset))
        else:
            spans.append(Preset_Span(start, end, preset))
    return tuple(Preset_Span(s.start_s, s.end_s, s.preset, 0 if i == 0 else min(
        STAGE_CROSSFADE_DURATION_S, s.end_s - s.start_s, spans[i - 1].end_s - spans[i - 1].start_s))
        for i, s in enumerate(spans))


def preset_gains(plan, time):
    """Scalar reference; the renderer evaluates these same ramps per sample."""
    gains = {name: 0.0 for name in PRESETS}
    for i, span in enumerate(plan):
        start, end = span.start_s, span.end_s
        fade_in = span.crossfade_in_s
        fade_out = plan[i + 1].crossfade_in_s if i + 1 < len(plan) else 0
        if start <= time <= end:
            gain = 1.0
        else:
            gain = 0.0
        if fade_in and start - fade_in / 2 <= time < start + fade_in / 2:
            gain = (time - start + fade_in / 2) / fade_in
        if fade_out and end - fade_out / 2 <= time <= end + fade_out / 2:
            gain = (end + fade_out / 2 - time) / fade_out
        gains[span.preset] += gain
    return gains
