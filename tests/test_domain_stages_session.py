"""Unit tests for backend.domain.stages and backend.domain.session (task 2.7).

Requirements 2.3 (Stage_Name_Mapping), 2.4 (uncovered intervals are unknown),
2.5 / 2.6 / 2.10 (sleep onset and final wake), and 2.8 (overlap resolution).
Basic clipping and overlap cases live in test_stage_segments.py; the overlap
examples here cover the remaining shapes (containment, identical intervals,
adjacency, gaps, mixed UTC offsets).
"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from backend.domain.session import SleepSession
from backend.domain.stages import (
    DOMINANT_STAGE_ORDER,
    STAGE_NAME_MAPPING,
    Sleep_Stage as S,
    Stage_Interval,
    Stage_Segment,
    build_stage_segments,
    map_stage_name,
)
from backend.domain.telemetry import Metric, TelemetryPoint

EST = timezone(timedelta(hours=-5))
T0 = datetime(2024, 3, 1, 23, 0, tzinfo=EST)


def t(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


def seg(start: float, end: float, stage: S, brief: bool = False) -> Stage_Segment:
    return Stage_Segment(t(start), t(end), stage, brief)


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


def session(stages=(), **kwargs) -> SleepSession:
    return SleepSession(t(0), t(480), "fitbit", stages, **kwargs)


# --- Stage_Name_Mapping (Requirement 2.3) --------------------------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("wake", S.awake),
        ("awake", S.awake),
        ("light", S.light),
        ("core", S.light),
        ("deep", S.deep),
        ("rem", S.rem),
        ("asleep", S.asleep),
        ("restless", S.restless),
        ("WAKE", S.awake),
        ("Deep", S.deep),
        ("REM", S.rem),
        ("Core", S.light),
        ("  light  ", S.light),
        ("\tRestless\n", S.restless),
        (" ASLEEP", S.asleep),
    ],
)
def test_map_stage_name_is_case_insensitive_and_trims(name, expected):
    assert map_stage_name(name) is expected


@pytest.mark.parametrize(
    "name",
    ["", "   ", "unknown", "nap", "light sleep", "re m", "deeep", "awake!", None, 3, b"deep"],
)
def test_map_stage_name_unknown_fallback(name):
    assert map_stage_name(name) is S.unknown


def test_stage_name_mapping_is_read_only():
    with pytest.raises(TypeError):
        STAGE_NAME_MAPPING["nap"] = S.asleep  # type: ignore[index]


def test_dominant_stage_order_lists_every_stage_once():
    assert DOMINANT_STAGE_ORDER[0] is S.awake
    assert sorted(DOMINANT_STAGE_ORDER) == sorted(S)
    assert len(DOMINANT_STAGE_ORDER) == len(S)


# --- Stage_Segment ----------------------------------------------------------------


def test_stage_segment_contains_is_half_open():
    s = seg(10, 20, S.deep)
    assert s.contains(t(10))
    assert s.contains(t(19.99))
    assert not s.contains(t(20))
    assert not s.contains(t(9.99))


@pytest.mark.parametrize(("start", "end"), [(10, 10), (20, 10)])
def test_stage_segment_rejects_non_positive_duration(start, end):
    with pytest.raises(ValueError):
        seg(start, end, S.light)


def test_stage_segment_rejects_naive_and_non_stage():
    with pytest.raises(ValueError):
        Stage_Segment(datetime(2024, 3, 1, 23), t(10), S.light)
    with pytest.raises(ValueError):
        Stage_Segment(t(0), datetime(2024, 3, 2, 1), S.light)
    with pytest.raises(TypeError):
        Stage_Segment(t(0), t(10), "light")  # type: ignore[arg-type]


def test_stage_segment_is_frozen_and_not_brief_by_default():
    s = seg(0, 10, S.awake)
    assert s.is_brief_awakening is False
    with pytest.raises(dataclasses.FrozenInstanceError):
        s.stage = S.deep  # type: ignore[misc]


# --- Overlap resolution (Requirement 2.8) --------------------------------------------


def test_later_starting_contained_interval_splits_the_outer_one():
    segments = build_stage_segments([iv(0, 60, S.light), iv(20, 30, S.rem)], t(0), t(480))
    assert summary(segments) == [
        (0, 20, S.light, False),
        (20, 30, S.rem, False),
        (30, 60, S.light, False),
    ]


def test_earlier_starting_interval_loses_even_when_listed_later():
    segments = build_stage_segments([iv(20, 30, S.rem), iv(0, 60, S.light)], t(0), t(480))
    assert summary(segments) == [
        (0, 20, S.light, False),
        (20, 30, S.rem, False),
        (30, 60, S.light, False),
    ]


def test_identical_intervals_resolve_to_later_source_order():
    segments = build_stage_segments([iv(0, 30, S.light), iv(0, 30, S.deep)], t(0), t(480))
    assert summary(segments) == [(0, 30, S.deep, False)]


def test_equal_start_later_source_wins_and_longer_interval_resumes():
    segments = build_stage_segments([iv(0, 10, S.deep), iv(0, 30, S.light)], t(0), t(480))
    assert summary(segments) == [(0, 30, S.light, False)]
    segments = build_stage_segments([iv(0, 30, S.light), iv(0, 10, S.deep)], t(0), t(480))
    assert summary(segments) == [(0, 10, S.deep, False), (10, 30, S.light, False)]


def test_brief_awakening_beats_later_starting_contained_interval():
    segments = build_stage_segments(
        [iv(10, 20, S.awake, brief=True), iv(12, 15, S.rem)], t(0), t(480)
    )
    assert summary(segments) == [(10, 20, S.awake, True)]


def test_brief_awakening_stays_identifiable_after_clipping():
    segments = build_stage_segments(
        [iv(-5, 2, S.awake, brief=True), iv(0, 30, S.light)], t(0), t(480)
    )
    assert summary(segments) == [(0, 2, S.awake, True), (2, 30, S.light, False)]


def test_adjacent_equal_stage_intervals_are_not_merged_and_gaps_are_kept():
    segments = build_stage_segments(
        [iv(0, 10, S.light), iv(10, 20, S.light), iv(30, 40, S.deep)], t(0), t(480)
    )
    assert summary(segments) == [
        (0, 10, S.light, False),
        (10, 20, S.light, False),
        (30, 40, S.deep, False),
    ]


def test_mixed_utc_offsets_are_compared_as_instants():
    cet = timezone(timedelta(hours=1))
    # Same instants as t(0)..t(30) and t(10)..t(20), expressed in another offset.
    outer = Stage_Interval(t(0).astimezone(cet), t(30).astimezone(cet), S.light)
    inner = Stage_Interval(t(10), t(20), S.rem)
    segments = build_stage_segments([outer, inner], t(0), t(480))
    assert summary(segments) == [
        (0, 10, S.light, False),
        (10, 20, S.rem, False),
        (20, 30, S.light, False),
    ]


def test_session_end_not_after_start_yields_no_segments():
    assert build_stage_segments([iv(0, 10, S.light)], t(10), t(0)) == []


def test_naive_session_bounds_raise():
    with pytest.raises(ValueError):
        build_stage_segments([], datetime(2024, 3, 1), t(10))


# Known issue, reported by task 2.7: when all datetimes share one ZoneInfo
# tzinfo, Python compares and subtracts them by wall-clock time and ignores
# `fold`. Across a DST fall-back, a segment from 01:50 EDT (05:50 UTC) to
# 01:10 EST (06:10 UTC) is 20 minutes long, but Stage_Segment rejects it and
# build_stage_segments discards it. Such datetimes appear whenever a UTC
# instant is converted with `.astimezone(ZoneInfo(...))` in the repeated hour.
_NY = ZoneInfo("America/New_York")
_FALL_A = datetime(2024, 11, 3, 5, 50, tzinfo=timezone.utc).astimezone(_NY)  # 01:50 EDT
_FALL_B = datetime(2024, 11, 3, 6, 10, tzinfo=timezone.utc).astimezone(_NY)  # 01:10 EST


@pytest.mark.xfail(strict=True, reason="same-ZoneInfo comparison ignores fold across DST fall-back")
def test_stage_segment_across_dst_fall_back_in_same_zoneinfo():
    segment = Stage_Segment(_FALL_A, _FALL_B, S.deep)
    assert segment.contains(datetime(2024, 11, 3, 6, 0, tzinfo=timezone.utc))


@pytest.mark.xfail(strict=True, reason="same-ZoneInfo comparison ignores fold across DST fall-back")
def test_build_stage_segments_across_dst_fall_back_in_same_zoneinfo():
    start = datetime(2024, 11, 3, 4, 0, tzinfo=timezone.utc).astimezone(_NY)
    end = datetime(2024, 11, 3, 8, 0, tzinfo=timezone.utc).astimezone(_NY)
    segments = build_stage_segments([Stage_Interval(_FALL_A, _FALL_B, S.deep)], start, end)
    assert len(segments) == 1


# --- SleepSession: sleep onset and final wake (Requirements 2.5, 2.6, 2.10) ------------


def test_sleep_onset_and_final_wake_skip_awake_and_unknown():
    s = session([
        seg(0, 20, S.awake),
        seg(20, 25, S.unknown),
        seg(25, 90, S.light),
        seg(90, 100, S.awake, brief=True),
        seg(100, 300, S.deep),
        seg(300, 420, S.rem),
        seg(420, 440, S.unknown),
        seg(440, 480, S.awake),
    ])
    assert s.sleep_onset_time == t(25)
    assert s.final_wake_time == t(420)


def test_classic_stages_count_toward_onset_and_final_wake():
    s = session([seg(5, 10, S.awake), seg(10, 200, S.asleep), seg(200, 210, S.restless)])
    assert s.sleep_onset_time == t(10)
    assert s.final_wake_time == t(210)


def test_single_sleep_segment_defines_both_times():
    s = session([seg(60, 61, S.rem)])
    assert (s.sleep_onset_time, s.final_wake_time) == (t(60), t(61))


@pytest.mark.parametrize(
    "stages",
    [
        [],
        [seg(0, 480, S.awake)],
        [seg(0, 100, S.unknown)],
        [seg(0, 100, S.awake), seg(100, 200, S.unknown), seg(200, 210, S.awake, brief=True)],
    ],
)
def test_onset_and_final_wake_undefined_without_sleep_segments(stages):
    s = session(stages)
    assert s.sleep_onset_time is None
    assert s.final_wake_time is None
    # The session itself is still a usable value (Requirement 2.10).
    assert s.stage_at(t(50)) in (S.awake, S.unknown)


# --- SleepSession: stage lookup and availability (Requirement 2.4) ------------------


def test_stage_at_uses_half_open_segments_and_unknown_for_gaps():
    s = session([seg(0, 10, S.awake), seg(10, 20, S.light), seg(30, 40, S.deep)])
    assert s.stage_at(t(0)) is S.awake
    assert s.stage_at(t(10)) is S.light  # start inclusive
    assert s.stage_at(t(19.5)) is S.light
    assert s.stage_at(t(20)) is S.unknown  # end exclusive, then a gap
    assert s.stage_at(t(25)) is S.unknown
    assert s.stage_at(t(39.9)) is S.deep
    assert s.stage_at(t(40)) is S.unknown
    assert s.stage_at(t(-1)) is S.unknown  # before the session
    assert s.stage_at(t(600)) is S.unknown  # after the session


def test_stage_at_without_stages_is_unknown_everywhere():
    s = session()
    for minutes in (0, 1, 240, 479.9):
        assert s.stage_at(t(minutes)) is S.unknown


def test_stage_at_accepts_other_utc_offsets():
    s = session([seg(10, 20, S.rem)])
    assert s.stage_at(t(15).astimezone(timezone.utc)) is S.rem


@pytest.mark.parametrize(
    ("stages", "has_data", "availability"),
    [
        ([], False, "none"),
        ([seg(0, 10, S.unknown)], False, "none"),
        ([seg(0, 10, S.awake)], True, "classic"),
        ([seg(0, 10, S.asleep), seg(10, 20, S.restless)], True, "classic"),
        ([seg(0, 10, S.awake), seg(10, 20, S.deep)], True, "stages"),
        ([seg(0, 10, S.asleep), seg(10, 20, S.rem)], True, "stages"),
    ],
)
def test_stage_data_flags(stages, has_data, availability):
    s = session(stages)
    assert s.has_stage_data is has_data
    assert s.stage_data_availability == availability


# --- SleepSession: value semantics ----------------------------------------------------


def test_session_stores_tuples_and_defaults():
    point = TelemetryPoint(t(5), "fitbit", Metric.heart_rate, 60.0, "bpm")
    s = SleepSession(t(0), t(480), "fitbit", [seg(0, 10, S.light)], [point])
    assert isinstance(s.stages, tuple) and isinstance(s.telemetry, tuple)
    assert s.telemetry == (point,)
    assert (s.log_id, s.is_main_sleep, s.session_hrv, s.source_file) == (None, False, None, None)


def test_session_is_frozen_and_compares_by_value():
    a = session([seg(0, 10, S.light)], log_id="42", is_main_sleep=True, session_hrv=35.0)
    b = session((seg(0, 10, S.light),), log_id="42", is_main_sleep=True, session_hrv=35.0)
    assert a == b
    assert a != session([seg(0, 10, S.deep)], log_id="42", is_main_sleep=True, session_hrv=35.0)
    with pytest.raises(dataclasses.FrozenInstanceError):
        a.source = "other"  # type: ignore[misc]
    # Derived values still work on the frozen instance (cached internally).
    assert a.sleep_onset_time == t(0) and a.sleep_onset_time == t(0)
