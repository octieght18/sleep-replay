"""Remaining design properties (3-10, 13-20), using independent generated inputs."""
import math
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from itertools import groupby
from zoneinfo import ZoneInfo

import pytest
from hypothesis import given, settings, strategies as st

from backend.domain.errors import Import_Report
from backend.domain.events import EVENT_MERGE_WINDOW, MAX_EVENT_COUNT, Event_Type
from backend.domain.stages import Sleep_Stage, Stage_Segment
from backend.domain.session import SleepSession
from backend.domain.telemetry import Metric, TelemetryPoint, CANONICAL_UNIT
from backend.domain.timeline import MAX_INTERPOLATION_GAP
from backend.ingestion.fitbit.sleep import parse_sleep_json
from backend.processing.aligner import align
from backend.processing.events import detect
from backend.processing.features import extract, movement_intensity_from_mean
from backend.processing.fingerprint import input_fingerprint
from backend.processing.resample import resample_continuous, sample_grid
from backend.processing.session_detector import attach_telemetry, dedup_and_merge

T0 = datetime(2024, 3, 1, 22, tzinfo=timezone.utc)
STAGES = tuple(Sleep_Stage)
offsets = st.integers(-720, 840).map(lambda m: timezone(timedelta(minutes=m)))


@st.composite
def nights(draw):
    duration = draw(st.integers(1201, 7200))
    end = T0 + timedelta(seconds=duration)
    boundaries = sorted(set(draw(st.lists(st.integers(1, duration - 1), max_size=10))))
    edges = [T0, *(T0 + timedelta(seconds=s) for s in boundaries), end]
    stages = [Stage_Segment(a, b, draw(st.sampled_from(STAGES))) for a, b in zip(edges, edges[1:])]
    measurements = draw(st.lists(st.tuples(
        st.integers(0, duration), st.sampled_from(tuple(Metric)), st.integers(0, 100),
        st.sampled_from(("fitbit", "sensorpush:a", "sensorpush:b"))), max_size=25))
    points = [TelemetryPoint(T0 + timedelta(seconds=s), source, metric, float(value), CANONICAL_UNIT[metric])
              for s, metric, value, source in measurements]
    return SleepSession(T0, end, "fake", tuple(stages), tuple(points))


# Feature: sleep-replay-data-pipeline, Property 3: Stage_Segment ordering and containment invariant
@settings(max_examples=100)
@given(st.lists(st.tuples(st.integers(-1000, 5000), st.integers(-20, 3000),
                        st.sampled_from(("deep", "light", "rem", "wake", "mystery")), st.booleans()), max_size=30))
def test_property_3_parsed_stage_containment(intervals):
    """Validates requirements 2.2, 2.9, 3.2, including clipped and brief entries."""
    data, short = [], []
    for offset, duration, stage, brief in intervals:
        entry = {"dateTime": (T0 + timedelta(seconds=offset)).isoformat(), "seconds": duration, "level": stage}
        (short if brief else data).append(entry)
    end = T0 + timedelta(hours=1)
    logs = [{"startTime": T0.isoformat(), "endTime": end.isoformat(),
             "levels": {"data": data, "shortData": short}}]
    (session,) = parse_sleep_json(json.dumps(logs), "sleep-2024-03-01.json", ZoneInfo("UTC"), Import_Report())
    for segment in session.stages:
        assert T0 <= segment.start_time < segment.end_time <= end
    assert all(a.end_time <= b.start_time for a, b in zip(session.stages, session.stages[1:]))


# Feature: sleep-replay-data-pipeline, Property 4: Session deduplication and merge fixpoint
@settings(max_examples=100)
@given(st.lists(st.tuples(st.integers(0, 30), st.integers(1, 15),
                        st.one_of(st.none(), st.sampled_from(("a", "b", "c"))), st.booleans()), max_size=25))
def test_property_4_session_fixpoint(inputs):
    """Validates requirements 8.5, 8.6: duplicates, touching, overlaps and chains."""
    candidates = []
    for i, (start, length, log_id, main) in enumerate(inputs):
        a, b = T0 + timedelta(minutes=start), T0 + timedelta(minutes=start + length)
        candidates.append(SleepSession(a, b, "fake", (Stage_Segment(a, b, STAGES[i % len(STAGES)]),),
                                       log_id=log_id, is_main_sleep=main, source_file=f"{i}.json"))
    first = dedup_and_merge(candidates)
    again = dedup_and_merge(first.sessions)
    assert again.sessions == first.sessions
    assert again.discarded_duplicates == 0 and not again.warnings
    assert all(a.end_time <= b.start_time for a, b in zip(first.sessions, first.sessions[1:]))
    assert dedup_and_merge(reversed(candidates)).sessions == first.sessions


# Feature: sleep-replay-data-pipeline, Property 5: Telemetry attachment boundary
@settings(max_examples=100)
@given(st.lists(st.tuples(st.integers(-1000, 5000), offsets), max_size=30), offsets)
def test_property_5_closed_instant_boundary(inputs, session_zone):
    """Validates requirements 8.10, 2.1 with offset-independent, inclusive endpoints."""
    end = T0 + timedelta(hours=1)
    session = SleepSession(T0.astimezone(session_zone), end.astimezone(session_zone), "fake")
    inputs = [(0, timezone.utc), (3600, timezone.utc), *inputs]
    points = [TelemetryPoint((T0 + timedelta(seconds=s)).astimezone(z), "fake", Metric.steps, 0, "steps/min")
              for s, z in inputs]
    assert attach_telemetry(session, points).telemetry == tuple(p for p, (s, _) in zip(points, inputs) if 0 <= s <= 3600)


# Feature: sleep-replay-data-pipeline, Property 6: Alignment is independent of the Display_Timezone
@settings(max_examples=100)
@given(nights(), offsets)
def test_property_6_timezone_independence(session, zone):
    """Validates requirement 9.5; changing presentation offsets preserves all instants."""
    other = replace(session, start_time=session.start_time.astimezone(zone), end_time=session.end_time.astimezone(zone),
                    telemetry=tuple(replace(p, timestamp=p.timestamp.astimezone(zone)) for p in session.telemetry),
                    stages=tuple(replace(s, start_time=s.start_time.astimezone(zone), end_time=s.end_time.astimezone(zone))
                                 for s in session.stages))
    assert align(session) == align(other)
    assert input_fingerprint(session) == input_fingerprint(other)


# Feature: sleep-replay-data-pipeline, Property 7: Aligned values stay within the measured range
@settings(max_examples=100)
@given(nights())
def test_property_7_measured_range(session):
    """Validates requirement 10.14, including missing values and multiple sensors."""
    timeline, _ = align(session)
    for metric in Metric:
        measured = [p.value for p in session.telemetry if p.metric is metric]
        for value, missing in zip(timeline.values[metric], timeline.missing[metric]):
            assert (value is None) == missing
            if not missing:
                assert min(measured) <= value <= max(measured)


# Feature: sleep-replay-data-pipeline, Property 8: Aligned_Timeline sample count
@settings(max_examples=100)
@given(st.integers(1, 100000000000), offsets)
def test_property_8_sample_count(duration_us, zone):
    """Validates requirement 10.15 over partial seconds and offset timestamps."""
    end = T0 + timedelta(microseconds=duration_us)
    timeline, _ = align(SleepSession(T0.astimezone(zone), end.astimezone(zone), "fake"))
    assert timeline.sample_count == math.ceil(duration_us / (timeline.resolution_s * 1000000))
    assert all(timeline.timestamps[i + 1] - timeline.timestamps[i] == timedelta(seconds=timeline.resolution_s)
               for i in range(timeline.sample_count - 1))


# Feature: sleep-replay-data-pipeline, Property 9: Alignment is confluent (order-independent)
@settings(max_examples=100)
@given(nights(), st.data())
def test_property_9_alignment_confluence(session, data):
    """Validates requirement 10.18 including report, duplicates and fingerprint."""
    points = data.draw(st.permutations(session.telemetry))
    stages = data.draw(st.permutations(session.stages))
    other = replace(session, telemetry=points, stages=stages)
    assert align(session) == align(other)
    assert input_fingerprint(session) == input_fingerprint(other)


# Feature: sleep-replay-data-pipeline, Property 10: Interpolation-gap boundary behavior
@settings(max_examples=100)
@given(st.sampled_from(tuple(MAX_INTERPOLATION_GAP)), st.integers(-1, 1), st.integers(1, 100))
def test_property_10_interpolation_boundary(metric, delta_us, value):
    """Validates requirements 10.8-10.10 at, below, and just above every default gap."""
    gap = MAX_INTERPOLATION_GAP[metric] + timedelta(microseconds=delta_us)
    end = T0 + gap + timedelta(seconds=120)
    grid = sample_grid(T0 - timedelta(seconds=60), end, 60)
    series = [(T0, 0.0), (T0 + gap, float(value))]
    values, missing, gaps = resample_continuous(metric, series, grid, end)
    # Samples at -60 and beyond the last measurement must not extrapolate.
    assert missing[0] and missing[-1]
    if delta_us <= 0:
        assert not missing[2] and not gaps
        assert values[2] == pytest.approx(value * 60 / gap.total_seconds())
    else:
        assert missing[2] and len(gaps) == 1 and gaps[0].elapsed == gap


# Feature: sleep-replay-data-pipeline, Property 13: Movement_Intensity is monotone and bounded
@settings(max_examples=100)
@given(st.floats(0, 10000, allow_nan=False), st.floats(0, 10000, allow_nan=False))
def test_property_13_movement_monotone(a, b):
    """Validates requirement 12.7 with saturation and the zero boundary."""
    a, b = sorted((a, b))
    assert 0 <= movement_intensity_from_mean(a) <= movement_intensity_from_mean(b) <= 1
    assert movement_intensity_from_mean(0) == movement_intensity_from_mean(None) == 0


# Feature: sleep-replay-data-pipeline, Property 14: Feature_Windows cover the session with required count
@settings(max_examples=100)
@given(nights(), st.sampled_from((30, 120, 180, 300, 600)))
def test_property_14_window_coverage(session, target):
    """Validates requirement 12.10 with non-divisible durations and sparse data."""
    timeline, _ = align(session)
    features, _, _ = extract(timeline, session, target)
    windows = features.windows
    assert len(windows) == math.ceil(target / .5)
    assert windows[0].start_time == session.start_time and windows[-1].end_time == session.end_time
    assert all(a.end_time == b.start_time for a, b in zip(windows, windows[1:]))
    assert all(w.end_time > w.start_time for w in windows)


# Feature: sleep-replay-data-pipeline, Property 15: Per-window numeric invariants
@settings(max_examples=100)
@given(nights())
def test_property_15_window_invariants(session):
    """Validates requirement 12.11, exercising computed statistics and empty windows."""
    timeline, _ = align(session)
    features, _, _ = extract(timeline, session, 30)
    for window in features.windows:
        assert math.fsum(window.stage_fractions.values()) == pytest.approx(1, abs=1e-6)
        assert 0 <= window.movement_intensity <= 1
        for stats in window.per_metric.values():
            assert 0 <= stats.missing_fraction <= 1
            if stats.mean is None:
                assert stats.minimum is stats.maximum is stats.slope is None
                assert stats.missing_fraction == 1
            else:
                assert stats.minimum <= stats.mean <= stats.maximum


# Feature: sleep-replay-data-pipeline, Property 16: Coarse_State length and minimum-run invariant
@settings(max_examples=100)
@given(nights())
def test_property_16_coarse_states(session):
    """Validates requirement 12.12 on extracted states with many short runs."""
    timeline, _ = align(session)
    features, states, _ = extract(timeline, session, 30)
    assert len(states) == len(features)
    assert all(len(list(run)) * .5 >= 1 for _, run in groupby(states))


def detected(session, target=30):
    timeline, _ = align(session)
    features, states, _ = extract(timeline, session, target)
    return detect(session, timeline, features, states, target, ZoneInfo("America/New_York"))


# Feature: sleep-replay-data-pipeline, Property 17: Night_Event Replay_Time bounds
@settings(max_examples=100)
@given(nights())
def test_property_17_event_bounds(session):
    """Validates requirement 13.13 and the linear replay mapping."""
    for event in detected(session):
        assert session.start_time <= event.night_time <= session.end_time
        assert 0 <= event.replay_time_s <= 30
        assert event.replay_time_s == pytest.approx((event.night_time - session.start_time).total_seconds()
                                                    / ((session.end_time - session.start_time).total_seconds() / 30))


# Feature: sleep-replay-data-pipeline, Property 18: Night_Event magnitude bounds
@settings(max_examples=100)
@given(nights())
def test_property_18_event_magnitude(session):
    """Validates requirement 13.14 across stage, movement and environmental data."""
    for event in detected(session):
        assert 0 <= event.magnitude <= 1 and math.isfinite(event.magnitude)
        if event.type in (Event_Type.sleep_onset, Event_Type.awakening):
            assert event.magnitude == 1


# Feature: sleep-replay-data-pipeline, Property 19: Night_Event merge window and count cap
@settings(max_examples=100)
@given(nights())
def test_property_19_event_merge_cap(session):
    """Validates requirements 13.9, 13.10 and mandatory endpoint retention."""
    events = detected(session)
    assert len(events) <= MAX_EVENT_COUNT
    for event_type in Event_Type:
        typed = [e for e in events if e.type is event_type]
        assert all(b.night_time - a.night_time > EVENT_MERGE_WINDOW for a, b in zip(typed, typed[1:]))
    if session.sleep_onset_time is not None:
        assert sum(e.type is Event_Type.sleep_onset for e in events) == 1
        assert sum(e.type is Event_Type.awakening for e in events) == 1


# Feature: sleep-replay-data-pipeline, Property 20: Night_Event ordering
@settings(max_examples=100)
@given(nights())
def test_property_20_event_order(session):
    """Validates requirement 13.12 and labels in Display_Timezone."""
    events = detected(session)
    assert events == sorted(events, key=lambda e: e.sort_key)
    for event in events:
        assert event.label == f"{event.night_time.astimezone(ZoneInfo('America/New_York')):%H:%M} {event.type.value}"
