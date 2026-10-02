"""Immutable vocabulary at the features-to-audio boundary (seconds of replay time)."""
from __future__ import annotations

import math
from bisect import bisect_right
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Mapping


class Mapping_Target(StrEnum):
    pulse_rate = "pulse_rate"
    rhythmic_density = "rhythmic_density"
    intensity = "intensity"
    transient_density = "transient_density"
    brightness = "brightness"
    texture_density = "texture_density"
    modulation = "modulation"
    none = "none"


SOUND_PARAMETERS = tuple(t for t in Mapping_Target if t is not Mapping_Target.none)
FAST_PARAMETERS = frozenset(SOUND_PARAMETERS[:4])
SLOW_PARAMETERS = frozenset(SOUND_PARAMETERS[4:])
PARAMETER_RATE_LIMITS = MappingProxyType({t: 0.5 if t in FAST_PARAMETERS else 0.2 for t in SOUND_PARAMETERS})


@dataclass(frozen=True)
class Breakpoint:
    t: float
    value: float

    def __post_init__(self):
        if not math.isfinite(self.t) or self.t < 0 or not math.isfinite(self.value) or not 0 <= self.value <= 1:
            raise ValueError("breakpoints need finite nonnegative times and values in [0, 1]")


@dataclass(frozen=True)
class Parameter_Trajectory:
    breakpoints: tuple[Breakpoint, ...]
    _times: tuple[float, ...] = field(init=False, repr=False, compare=False)

    def __post_init__(self):
        points = tuple(self.breakpoints)
        if not points or any(b.t <= a.t for a, b in zip(points, points[1:])):
            raise ValueError("trajectory needs strictly ascending breakpoints")
        object.__setattr__(self, "breakpoints", points)
        object.__setattr__(self, "_times", tuple(p.t for p in points))

    def value_at(self, t: float) -> float:
        i = bisect_right(self._times, t)
        if i == 0:
            return self.breakpoints[0].value
        if i == len(self.breakpoints):
            return self.breakpoints[-1].value
        a, b = self.breakpoints[i - 1:i + 1]
        return a.value + (b.value - a.value) * (t - a.t) / (b.t - a.t)


@dataclass(frozen=True)
class Transient_Event:
    onset_s: float
    amplitude: float
    kind: str


@dataclass(frozen=True)
class Note_Event:
    onset_s: float
    duration_s: float
    pitch_hz: float
    amplitude: float
    layer: str


@dataclass(frozen=True)
class Onset_Gesture:
    event_type: str
    start_s: float
    end_s: float


@dataclass(frozen=True)
class Preset_Span:
    start_s: float
    end_s: float
    preset: str
    crossfade_in_s: float = 0.0


@dataclass(frozen=True)
class Render_Plan:
    target_duration_s: int
    trajectories: Mapping[Mapping_Target, Parameter_Trajectory]
    transients: tuple[Transient_Event, ...]
    gestures: tuple[Onset_Gesture, ...]
    preset_plan: tuple[Preset_Span, ...]
    seed: int
    notes: tuple[Note_Event, ...] = ()
    sound_style: str = "music"
    nature_controls: Mapping[str, Parameter_Trajectory] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "trajectories", MappingProxyType(dict(self.trajectories)))
        if self.sound_style not in ("music", "nature"):
            raise ValueError("sound style must be music or nature")
        object.__setattr__(self, "nature_controls", MappingProxyType(dict(self.nature_controls)))
