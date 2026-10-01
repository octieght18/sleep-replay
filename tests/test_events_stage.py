"""Sanity tests for stage-based event candidates (task 14.1, Req 13.1-13.4, 13.14, 13.15)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from backend.domain.events import Event_Type
from backend.domain.session import SleepSession
from backend.domain.stages import Sleep_Stage, Stage_Segment
from backend.domain.timeline import Aligned_Timeline, sample_timestamps
from backend.processing.events import detect_stage_events, stage_runs
from backend.processing.resample import assign_stages

T0 = datetime(2024, 3, 1, 22, 0, tzinfo=timezone.utc)
RES = 60


def _build(plan: list[tuple[Sleep_Stage, int]], tail_min: int = 0):
    """Session of consecutive (stage, minutes) segments, plus an uncovered tail."""
    segments, t = [], T0
    for stage, minutes in plan:
        end = t + timedelta(minutes=minutes)
        segments.append(Stage_Segment(t, end, stage))
        t = end
    end_time = t + timedelta(minutes=tail_min)
    session = SleepSession(T0, end_time, "test", stages=segments)
    grid = sample_timestamps(T0, end_time, RES)
    timeline = Aligned_Timeline(
        T0, end_time, RES, grid, {}, {}, assign_stages(segments, grid)
    )
    return session, timeline


def _at(minutes: int) -> datetime:
    return T0 + timedelta(minutes=minutes)


def test_stage_runs_merge_adjacent_equal_segments() -> None:
    _, timeline = _build([(Sleep_Stage.light, 5), (Sleep_Stage.light, 7), (Sleep_Stage.deep, 3)])
    runs = stage_runs(timeline)
    assert [(r.stage, r.duration) for r in runs] == [
        (Sleep_Stage.light, timedelta(minutes=12)),
        (Sleep_Stage.deep, timedelta(minutes=3)),
    ]
    assert runs[-1].end_time == timeline.end_time


def test_stage_events_and_magnitudes() -> None:
    session, timeline = _build(
        [
            (Sleep_Stage.awake, 10),  # before onset: no awake event
            (Sleep_Stage.light, 30),  # starts at onset: no transition event
            (Sleep_Stage.deep, 15),   # transition, 15/20 = 0.75
            (Sleep_Stage.awake, 4),   # too short
            (Sleep_Stage.rem, 9),     # too short
            (Sleep_Stage.awake, 6),   # awake, 6/10 = 0.6
            (Sleep_Stage.rem, 25),    # transition, capped at 1.0
            (Sleep_Stage.awake, 20),  # after final wake: no awake event
        ]
    )
    got = [(c.type, c.night_time, c.magnitude) for c in detect_stage_events(session, timeline)]
    assert got == [
        (Event_Type.sleep_onset, _at(10), 1.0),
        (Event_Type.deep_sleep, _at(40), 0.75),
        (Event_Type.awake, _at(68), 0.6),
        (Event_Type.rem, _at(74), 1.0),
        (Event_Type.awakening, _at(99), 1.0),
    ]


def test_no_sleep_stage_yields_no_stage_events() -> None:
    session, timeline = _build([(Sleep_Stage.awake, 30), (Sleep_Stage.unknown, 30)])
    assert detect_stage_events(session, timeline) == []
