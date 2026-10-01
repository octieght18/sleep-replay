"""Sanity tests for build_stage_segments (task 2.6).

Requirements 2.2, 2.8, 2.9, 3.3, 3.4. Fuller domain tests come in task 2.7.
"""

from datetime import datetime, timedelta, timezone

import pytest

from backend.domain.stages import (
    Sleep_Stage as S,
    Stage_Interval,
    build_stage_segments,
)

T0 = datetime(2024, 3, 1, 23, 0, tzinfo=timezone(timedelta(hours=-5)))


def t(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


def iv(start: float, end: float, stage: S, brief: bool = False) -> Stage_Interval:
    return Stage_Interval(t(start), t(end), stage, brief)


def summary(segments):
    return [
        ((s.start_time - T0) / timedelta(minutes=1),
         (s.end_time - T0) / timedelta(minutes=1),
         s.stage,
         s.is_brief_awakening)
        for s in segments
    ]


def test_clips_to_session_and_discards_empty():
    segments = build_stage_segments(
        [
            iv(-10, 5, S.light),   # clipped at start
            iv(5, 5, S.deep),      # zero length
            iv(8, 6, S.rem),       # negative length
            iv(55, 70, S.rem),     # clipped at end
            iv(80, 90, S.deep),    # entirely outside
        ],
        t(0),
        t(60),
    )
    assert summary(segments) == [(0, 5, S.light, False), (55, 60, S.rem, False)]


def test_brief_awakening_splits_underlying_segment():
    segments = build_stage_segments(
        [iv(0, 30, S.deep), iv(10, 11, S.awake, brief=True)], t(0), t(60)
    )
    assert summary(segments) == [
        (0, 10, S.deep, False),
        (10, 11, S.awake, True),
        (11, 30, S.deep, False),
    ]


def test_brief_awakening_wins_even_when_starting_earlier():
    segments = build_stage_segments(
        [iv(5, 12, S.awake, brief=True), iv(10, 30, S.light)], t(0), t(60)
    )
    assert summary(segments) == [(5, 12, S.awake, True), (12, 30, S.light, False)]


def test_later_start_wins_then_later_source_order():
    segments = build_stage_segments(
        [iv(0, 20, S.light), iv(10, 30, S.rem), iv(10, 15, S.deep)], t(0), t(60)
    )
    assert summary(segments) == [
        (0, 10, S.light, False),
        (10, 15, S.deep, False),
        (15, 30, S.rem, False),
    ]


def test_two_brief_awakenings_fall_back_to_later_start():
    segments = build_stage_segments(
        [iv(0, 10, S.awake, brief=True), iv(5, 8, S.restless, brief=True)],
        t(0),
        t(60),
    )
    assert summary(segments) == [
        (0, 5, S.awake, True),
        (5, 8, S.restless, True),
        (8, 10, S.awake, True),
    ]


def test_output_is_ordered_and_non_overlapping_for_shuffled_input():
    intervals = [iv(40, 50, S.rem), iv(0, 45, S.light), iv(20, 21, S.awake, True)]
    segments = build_stage_segments(intervals, t(0), t(60))
    for prev, cur in zip(segments, segments[1:]):
        assert cur.start_time >= prev.end_time
    assert summary(segments) == [
        (0, 20, S.light, False),
        (20, 21, S.awake, True),
        (21, 40, S.light, False),
        (40, 50, S.rem, False),
    ]


def test_empty_session_and_naive_timestamps():
    assert build_stage_segments([iv(0, 10, S.light)], t(10), t(10)) == []
    assert build_stage_segments([], t(0), t(10)) == []
    with pytest.raises(ValueError):
        build_stage_segments(
            [Stage_Interval(datetime(2024, 3, 1), datetime(2024, 3, 2), S.light)],
            t(0),
            t(10),
        )
