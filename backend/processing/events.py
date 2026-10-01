"""Event_Detector: detection of Night_Events (Requirement 13).

The detector is a set of pure functions. Each family of detectors returns
:class:`Event_Candidate` values (type, UTC Night_Time, raw magnitude) rather
than :class:`~backend.domain.events.Night_Event`, because a Night_Event needs a
Replay_Time and a Display_Timezone label at construction. The final
``detect`` step (task 14.4) merges, caps, orders, and turns candidates into
Night_Events via ``Night_Compression.replay_time`` and the label rule.

Detector families:

- stage-based (this module, :func:`detect_stage_events`): sleep onset,
  awake, awakening, light/deep/REM transitions (Req 13.1-13.4, 13.14, 13.15);
- movement / restless period (task 14.2);
- environmental rising / falling (task 14.3).

Stage_Run source
----------------
Stage_Runs are built from the Aligned_Timeline's per-sample stages, not from
the session's Stage_Segments, because the glossary defines a Stage_Run as "a
maximal run of consecutive Aligned_Timeline samples with the same
Sleep_Stage; its duration is the sample count multiplied by the
Timeline_Resolution". Adjacent equal-stage segments therefore form one run.
Sleep onset and final wake times, by contrast, come from the SleepSession
(Requirement 2.5, 2.6), so onset/awakening Night_Times are exact and not
snapped to the sample grid.

Imports only ``backend.domain``, the standard library, and other
``backend.processing`` modules (Dependency_Rules, Requirement 17.2).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta

from backend.domain.events import (
    MAJOR_TRANSITION_MIN_DURATION,
    STAGE_TRANSITION_EVENT_TYPE,
    WAKE_EVENT_MIN_DURATION,
    Event_Type,
)
from backend.domain.session import SleepSession
from backend.domain.stages import Sleep_Stage
from backend.domain.timeline import Aligned_Timeline
from backend.processing.timezones import to_utc

__all__ = [
    "Event_Candidate",
    "Stage_Run",
    "stage_runs",
    "duration_magnitude",
    "detect_stage_events",
]


@dataclass(frozen=True)
class Event_Candidate:  # noqa: N801 - name follows the spec glossary style
    """A detected event before merging, capping, Replay_Time, and labeling.

    - ``type``: the Event_Type (one Event_Vocabulary description).
    - ``night_time``: the Night_Time as a UTC instant.
    - ``magnitude``: the Requirement 13.14 magnitude, in [0.0, 1.0].

    Internal to the Event_Detector; ``detect`` (task 14.4) converts retained
    candidates into Night_Events.
    """

    type: Event_Type
    night_time: datetime
    magnitude: float

    def __post_init__(self) -> None:
        if not isinstance(self.type, Event_Type):
            raise TypeError("type must be an Event_Type")
        object.__setattr__(self, "night_time", to_utc(self.night_time))
        m = self.magnitude
        if isinstance(m, bool) or not isinstance(m, (int, float)) or not math.isfinite(m):
            raise ValueError("magnitude must be a finite number")
        if not 0.0 <= m <= 1.0:
            raise ValueError("magnitude must lie in [0.0, 1.0]")
        object.__setattr__(self, "magnitude", float(m))


@dataclass(frozen=True)
class Stage_Run:  # noqa: N801 - name follows the spec glossary
    """A maximal run of consecutive Aligned_Timeline samples with one Sleep_Stage.

    - ``sample_start`` / ``sample_stop``: half-open sample index range.
    - ``start_time``: UTC timestamp of the first sample.
    - ``end_time``: UTC end of the last sample's Sample_Interval (the next
      sample timestamp, or the session end_time for the final run).
    - ``duration``: sample count x Timeline_Resolution (glossary definition).
      Equals ``end_time - start_time`` except possibly for the final run,
      whose last Sample_Interval may be shorter than the resolution.
    """

    stage: Sleep_Stage
    sample_start: int
    sample_stop: int
    start_time: datetime
    end_time: datetime
    duration: timedelta

    @property
    def sample_count(self) -> int:
        return self.sample_stop - self.sample_start


def stage_runs(timeline: Aligned_Timeline) -> tuple[Stage_Run, ...]:
    """Stage_Runs of ``timeline`` in chronological order (empty for no samples)."""
    stages = timeline.stages
    n = len(stages)
    step = timedelta(seconds=timeline.resolution_s)
    runs: list[Stage_Run] = []
    i = 0
    while i < n:
        j = i + 1
        while j < n and stages[j] is stages[i]:
            j += 1
        runs.append(
            Stage_Run(
                stage=stages[i],
                sample_start=i,
                sample_stop=j,
                start_time=to_utc(timeline.timestamps[i]),
                end_time=to_utc(timeline.sample_interval(j - 1)[1]),
                duration=(j - i) * step,
            )
        )
        i = j
    return tuple(runs)


def duration_magnitude(duration: timedelta, min_duration: timedelta) -> float:
    """``min(1.0, duration / (2 * min_duration))`` (Requirement 13.14), floored at 0."""
    return max(0.0, min(1.0, duration / (2 * min_duration)))


def detect_stage_events(
    session: SleepSession, timeline: Aligned_Timeline
) -> list[Event_Candidate]:
    """Stage-based candidates in chronological order (Req 13.1-13.4, 13.14, 13.15).

    - sleep onset (1.0) at ``session.sleep_onset_time``;
    - awakening (1.0) at ``session.final_wake_time``;
    - awake at the start of each awake Stage_Run that starts strictly after
      the onset, ends at or before the final wake, and lasts at least
      Wake_Event_Min_Duration; magnitude ``min(1, dur / (2 * 5 min))``;
    - light sleep / deep sleep / REM at the start of each light/deep/rem
      Stage_Run that starts strictly after the onset and lasts at least
      Major_Transition_Min_Duration; magnitude ``min(1, dur / (2 * 10 min))``.

    A session without any Stage_Segment that is neither awake nor unknown has
    no onset or final wake and yields no stage-based candidates (Req 13.15).
    Same-type merging and the event cap are applied later (Req 13.9, 13.10).
    """
    onset = session.sleep_onset_time
    final_wake = session.final_wake_time
    if onset is None or final_wake is None:
        return []
    onset = to_utc(onset)
    final_wake = to_utc(final_wake)

    candidates = [
        Event_Candidate(Event_Type.sleep_onset, onset, 1.0),
        Event_Candidate(Event_Type.awakening, final_wake, 1.0),
    ]
    for run in stage_runs(timeline):
        if run.start_time <= onset:
            continue
        if run.stage is Sleep_Stage.awake:
            if run.end_time <= final_wake and run.duration >= WAKE_EVENT_MIN_DURATION:
                candidates.append(
                    Event_Candidate(
                        Event_Type.awake,
                        run.start_time,
                        duration_magnitude(run.duration, WAKE_EVENT_MIN_DURATION),
                    )
                )
        elif run.stage in STAGE_TRANSITION_EVENT_TYPE:
            if run.duration >= MAJOR_TRANSITION_MIN_DURATION:
                candidates.append(
                    Event_Candidate(
                        STAGE_TRANSITION_EVENT_TYPE[run.stage],
                        run.start_time,
                        duration_magnitude(run.duration, MAJOR_TRANSITION_MIN_DURATION),
                    )
                )
    candidates.sort(key=lambda c: c.night_time)
    return candidates
