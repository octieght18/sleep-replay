"""Night_Event types, the Event_Vocabulary, and event detection defaults.

Requirements 13.1–13.16 and the Default Parameter Values table (Wake,
Major_Transition, and Restless durations; Movement thresholds; environmental
thresholds and spans; merge window; Max_Event_Count). Imports only the
standard library and other ``backend.domain`` modules (Dependency_Rules,
Requirement 17.2).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from types import MappingProxyType
from typing import Mapping

from backend.domain.stages import Sleep_Stage
from backend.domain.telemetry import Metric

__all__ = [
    "Event_Type",
    "EVENT_VOCABULARY_ORDER",
    "EVENT_VOCABULARY_RANK",
    "STAGE_TRANSITION_EVENT_TYPE",
    "ENVIRONMENTAL_METRICS",
    "ENVIRONMENTAL_EVENT_TYPE",
    "WAKE_EVENT_MIN_DURATION",
    "MAJOR_TRANSITION_MIN_DURATION",
    "MOVEMENT_TRANSIENT_THRESHOLD",
    "MOVEMENT_BURST_THRESHOLD",
    "RESTLESS_THRESHOLD",
    "RESTLESS_MIN_DURATION",
    "ENVIRONMENTAL_CHANGE_THRESHOLD",
    "ENVIRONMENTAL_CHANGE_SPAN",
    "EVENT_MERGE_WINDOW",
    "MAX_EVENT_COUNT",
    "Night_Event",
]


class Event_Type(str, Enum):
    """Night_Event type; each value is exactly one Event_Vocabulary description (Req 13.11)."""

    sleep_onset = "sleep onset"
    awake = "awake"
    awakening = "awakening"
    light_sleep = "light sleep"
    deep_sleep = "deep sleep"
    rem = "REM"
    movement = "movement"
    restless_period = "restless period"
    temperature_rising = "temperature rising"
    temperature_falling = "temperature falling"
    humidity_rising = "humidity rising"
    humidity_falling = "humidity falling"
    pressure_rising = "pressure rising"
    pressure_falling = "pressure falling"

    @property
    def description(self) -> str:
        """The Event_Vocabulary description used in labels."""
        return self.value


#: Event_Vocabulary in glossary order; tie-break for equal Night_Times (Req 13.12).
EVENT_VOCABULARY_ORDER: tuple[Event_Type, ...] = (
    Event_Type.sleep_onset,
    Event_Type.awake,
    Event_Type.awakening,
    Event_Type.light_sleep,
    Event_Type.deep_sleep,
    Event_Type.rem,
    Event_Type.movement,
    Event_Type.restless_period,
    Event_Type.temperature_rising,
    Event_Type.temperature_falling,
    Event_Type.humidity_rising,
    Event_Type.humidity_falling,
    Event_Type.pressure_rising,
    Event_Type.pressure_falling,
)

#: Position of each Event_Type in :data:`EVENT_VOCABULARY_ORDER`.
EVENT_VOCABULARY_RANK: Mapping[Event_Type, int] = MappingProxyType(
    {event_type: rank for rank, event_type in enumerate(EVENT_VOCABULARY_ORDER)}
)

#: Stage-transition event type per Stage_Run stage (Requirement 13.4).
STAGE_TRANSITION_EVENT_TYPE: Mapping[Sleep_Stage, Event_Type] = MappingProxyType(
    {
        Sleep_Stage.light: Event_Type.light_sleep,
        Sleep_Stage.deep: Event_Type.deep_sleep,
        Sleep_Stage.rem: Event_Type.rem,
    }
)

#: Metrics evaluated for environmental change (Requirement 13.7).
ENVIRONMENTAL_METRICS: tuple[Metric, ...] = (
    Metric.temperature,
    Metric.humidity,
    Metric.pressure,
)

#: Environmental event type per (metric, rising) (Requirement 13.7).
ENVIRONMENTAL_EVENT_TYPE: Mapping[tuple[Metric, bool], Event_Type] = MappingProxyType(
    {
        (Metric.temperature, True): Event_Type.temperature_rising,
        (Metric.temperature, False): Event_Type.temperature_falling,
        (Metric.humidity, True): Event_Type.humidity_rising,
        (Metric.humidity, False): Event_Type.humidity_falling,
        (Metric.pressure, True): Event_Type.pressure_rising,
        (Metric.pressure, False): Event_Type.pressure_falling,
    }
)

# --- Detection defaults (Default Parameter Values) --------------------------

#: Minimum awake Stage_Run length for a wake event (Req 13.2, 13.14).
WAKE_EVENT_MIN_DURATION: timedelta = timedelta(minutes=5)

#: Minimum light/deep/rem Stage_Run length for a stage transition event (Req 13.4, 13.14).
MAJOR_TRANSITION_MIN_DURATION: timedelta = timedelta(minutes=10)

#: Movement_Intensity at which movement Transient_Events are triggered (used by sonification).
MOVEMENT_TRANSIENT_THRESHOLD: float = 0.1

#: Movement_Intensity threshold of a movement event's Movement_Run (Req 13.5).
MOVEMENT_BURST_THRESHOLD: float = 0.6

#: Movement_Intensity threshold of a Restless_Period (Req 13.6).
RESTLESS_THRESHOLD: float = 0.3

#: Minimum Night_Time span of a Restless_Period (Req 13.6).
RESTLESS_MIN_DURATION: timedelta = timedelta(minutes=10)

#: Environmental change threshold in the Canonical_Unit: °C, percentage points, hPa (Req 13.7).
ENVIRONMENTAL_CHANGE_THRESHOLD: Mapping[Metric, float] = MappingProxyType(
    {
        Metric.temperature: 1.0,
        Metric.humidity: 5.0,
        Metric.pressure: 1.0,
    }
)

#: Environmental change time span per metric (Req 13.7).
ENVIRONMENTAL_CHANGE_SPAN: Mapping[Metric, timedelta] = MappingProxyType(
    {
        Metric.temperature: timedelta(minutes=30),
        Metric.humidity: timedelta(minutes=30),
        Metric.pressure: timedelta(hours=3),
    }
)

#: Same-type event merge window of Night_Time (Req 13.9).
EVENT_MERGE_WINDOW: timedelta = timedelta(minutes=15)

#: Maximum number of Night_Events kept after merging (Req 13.10).
MAX_EVENT_COUNT: int = 12

_LABEL_TIME = re.compile(r"(?:[01]\d|2[0-3]):[0-5]\d")


@dataclass(frozen=True)
class Night_Event:
    """A detected notable moment of the night (Requirement 13.11).

    - ``type``: the Event_Type (exactly one Event_Vocabulary description).
    - ``night_time``: timezone-aware Night_Time within the SleepSession.
    - ``replay_time_s``: Replay_Time via the Requirement 11 mapping, >= 0 (Req 13.13).
    - ``magnitude``: in [0.0, 1.0] (Req 13.14).
    - ``label``: ``"HH:MM <description>"``, 24-hour Night_Time in the
      Display_Timezone truncated to the minute (e.g. ``"02:17 restless period"``).

    Construction validates these field-level constraints; session and
    Target_Duration bounds are the Event_Detector's responsibility.
    """

    type: Event_Type
    night_time: datetime
    replay_time_s: float
    magnitude: float
    label: str

    def __post_init__(self) -> None:
        if not isinstance(self.type, Event_Type):
            raise TypeError("type must be an Event_Type")
        if not isinstance(self.night_time, datetime) or self.night_time.utcoffset() is None:
            raise ValueError("night_time must be a timezone-aware datetime")
        for name in ("replay_time_s", "magnitude"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f"{name} must be a finite number")
        if self.replay_time_s < 0:
            raise ValueError("replay_time_s must be >= 0")
        if not 0.0 <= self.magnitude <= 1.0:
            raise ValueError("magnitude must lie in [0.0, 1.0]")
        if not isinstance(self.label, str):
            raise TypeError("label must be a string")
        time_part, sep, description = self.label.partition(" ")
        if sep != " " or not _LABEL_TIME.fullmatch(time_part) or description != self.type.value:
            raise ValueError(
                f"label must be 'HH:MM {self.type.value}', got {self.label!r}"
            )

    @property
    def description(self) -> str:
        """The Event_Vocabulary description of this event."""
        return self.type.value

    @property
    def sort_key(self) -> tuple[datetime, int]:
        """Ascending Night_Time, ties by Event_Vocabulary order (Req 13.12)."""
        return (self.night_time.astimezone(timezone.utc), EVENT_VOCABULARY_RANK[self.type])
