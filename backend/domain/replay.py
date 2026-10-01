"""Replay metadata; deliberately excludes raw telemetry and generation time."""
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Mapping, TypeAlias

from backend.domain.mapping import Mapping_Config

Coarse_State_Segment: TypeAlias = tuple[float, float, str]
Environmental_Window: TypeAlias = tuple[float | None, float | None, float | None]


@dataclass(frozen=True)
class Night_Event_Record:
    type: str
    night_time: datetime
    replay_time_s: float
    magnitude: float
    label: str


@dataclass(frozen=True)
class Metric_Availability:
    status: str
    missing_fraction: float


@dataclass(frozen=True)
class Replay_Manifest:
    session_start: datetime
    session_end: datetime
    display_timezone: str
    target_duration_s: int
    compression_ratio: float
    timeline_resolution_s: int
    coarse_states: tuple[Coarse_State_Segment, ...]
    night_events: tuple[Night_Event_Record, ...]
    environmental_windows: tuple[Environmental_Window, ...]
    availability: Mapping[str, Metric_Availability]
    unavailable_metrics: tuple[str, ...]
    mapping_config: Mapping_Config
    random_seed: int
    input_fingerprint: str
    version: str
    warnings: tuple[str, ...]

    def __post_init__(self):
        object.__setattr__(self, "availability", MappingProxyType(dict(self.availability)))
