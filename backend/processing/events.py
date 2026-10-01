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
from bisect import bisect_left, bisect_right
from collections import deque
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Sequence
from zoneinfo import ZoneInfo

from backend.domain.events import (
    MAJOR_TRANSITION_MIN_DURATION,
    STAGE_TRANSITION_EVENT_TYPE,
    WAKE_EVENT_MIN_DURATION,
    Event_Type,
    Night_Event,
    ENVIRONMENTAL_METRICS,
    ENVIRONMENTAL_CHANGE_THRESHOLD,
    ENVIRONMENTAL_CHANGE_SPAN,
    ENVIRONMENTAL_EVENT_TYPE,
    EVENT_MERGE_WINDOW,
    EVENT_VOCABULARY_RANK,
    MAX_EVENT_COUNT,
    MOVEMENT_BURST_THRESHOLD,
    RESTLESS_THRESHOLD,
    RESTLESS_MIN_DURATION,
)
from backend.domain.features import Coarse_State, Feature_Series, SMOOTHING_WINDOW
from backend.domain.session import SleepSession
from backend.domain.stages import Sleep_Stage
from backend.domain.telemetry import Metric
from backend.domain.timeline import Aligned_Timeline
from backend.processing.timezones import to_utc
from backend.processing.compression import night_compression

__all__ = [
    "Event_Candidate",
    "Stage_Run",
    "stage_runs",
    "duration_magnitude",
    "detect_stage_events",
    "detect_movement_events",
    "smoothed_samples",
    "detect_environmental_events",
    "merge_and_cap",
    "detect",
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


def detect_movement_events(features: Feature_Series) -> list[Event_Candidate]:
    """Detect maximal movement runs, using elapsed Night_Time for restless duration."""
    windows = features.windows
    events: list[Event_Candidate] = []
    for threshold, event_type in (
        (MOVEMENT_BURST_THRESHOLD, Event_Type.movement),
        (RESTLESS_THRESHOLD, Event_Type.restless_period),
    ):
        i = 0
        while i < len(windows):
            if windows[i].movement_intensity < threshold:
                i += 1
                continue
            j = i + 1
            while j < len(windows) and windows[j].movement_intensity >= threshold:
                j += 1
            values = [w.movement_intensity for w in windows[i:j]]
            duration = to_utc(windows[j - 1].end_time) - to_utc(windows[i].start_time)
            if event_type is Event_Type.movement or duration >= RESTLESS_MIN_DURATION:
                magnitude = max(values) if event_type is Event_Type.movement else math.fsum(values) / len(values)
                events.append(Event_Candidate(event_type, windows[i].start_time, magnitude))
            i = j
    return events


def smoothed_samples(timeline: Aligned_Timeline, metric: Metric) -> tuple[float | None, ...]:
    """Centered, session-truncated smoothing; missing sample positions stay missing."""
    times = [to_utc(t) for t in timeline.timestamps]
    values = timeline.values.get(metric, (None,) * len(times))
    mask = timeline.missing.get(metric, (True,) * len(times))
    sums = [0.0]
    counts = [0]
    for value, missing in zip(values, mask):
        sums.append(sums[-1] + (0.0 if missing else value))
        counts.append(counts[-1] + (not missing))
    half = SMOOTHING_WINDOW[metric] / 2
    result = []
    for t, missing in zip(times, mask):
        lo, hi = bisect_left(times, t - half), bisect_right(times, t + half)
        count = counts[hi] - counts[lo]
        result.append((sums[hi] - sums[lo]) / count if count and not missing else None)
    return tuple(result)


def detect_environmental_events(timeline: Aligned_Timeline) -> list[Event_Candidate]:
    """Compare smoothed extrema within each span, rearming at the emitted sample."""
    events = []
    for metric in ENVIRONMENTAL_METRICS:
        lows, highs = deque(), deque()
        span = ENVIRONMENTAL_CHANGE_SPAN[metric]
        threshold = ENVIRONMENTAL_CHANGE_THRESHOLD[metric]
        for t, value in zip(timeline.timestamps, smoothed_samples(timeline, metric)):
            if value is None:
                continue
            for queue in (lows, highs):
                while queue and queue[0][0] < t - span:
                    queue.popleft()
            if lows:
                earlier = min((lows[0], highs[0]), key=lambda p: (-abs(value - p[1]), p[0]))
                delta = value - earlier[1]
                if abs(delta) >= threshold:
                    events.append(Event_Candidate(
                        ENVIRONMENTAL_EVENT_TYPE[metric, delta > 0], t,
                        min(1.0, abs(delta) / (2 * threshold)),
                    ))
                    lows.clear()
                    highs.clear()
            while lows and lows[-1][1] > value:
                lows.pop()
            while highs and highs[-1][1] < value:
                highs.pop()
            lows.append((t, value))
            highs.append((t, value))
    return events


def merge_and_cap(candidates: Sequence[Event_Candidate]) -> list[Event_Candidate]:
    """Merge against the retained event's time, then preserve endpoints when capping."""
    retained: list[Event_Candidate] = []
    last: dict[Event_Type, int] = {}
    key = lambda e: (e.night_time, EVENT_VOCABULARY_RANK[e.type])
    for event in sorted(candidates, key=key):
        index = last.get(event.type)
        if index is not None and event.night_time - retained[index].night_time <= EVENT_MERGE_WINDOW:
            retained[index] = replace(retained[index], magnitude=max(retained[index].magnitude, event.magnitude))
        else:
            last[event.type] = len(retained)
            retained.append(event)
    if len(retained) > MAX_EVENT_COUNT:
        endpoints = [e for e in retained if e.type in (Event_Type.sleep_onset, Event_Type.awakening)]
        others = [e for e in retained if e.type not in (Event_Type.sleep_onset, Event_Type.awakening)]
        others.sort(key=lambda e: (-e.magnitude, *key(e)))
        retained = endpoints + others[:MAX_EVENT_COUNT - len(endpoints)]
    return sorted(retained, key=key)


def detect(
    session: SleepSession, timeline: Aligned_Timeline, features: Feature_Series,
    coarse_states: Sequence[Coarse_State], target_duration: int, display_tz: ZoneInfo,
) -> list[Night_Event]:
    """Detect, merge, cap, map, and label events; an empty result is valid."""
    if len(coarse_states) != len(features):
        raise ValueError("one Coarse_State is required per Feature_Window")
    mapping = night_compression(session, target_duration)
    candidates = detect_stage_events(session, timeline)
    candidates.extend(detect_movement_events(features))
    candidates.extend(detect_environmental_events(timeline))
    candidates = [e for e in candidates if mapping.start_time <= e.night_time <= mapping.end_time]
    return [Night_Event(
        e.type, e.night_time, mapping.replay_time(e.night_time), min(1.0, max(0.0, e.magnitude)),
        f"{e.night_time.astimezone(display_tz):%H:%M} {e.type.description}",
    ) for e in merge_and_cap(candidates)]
