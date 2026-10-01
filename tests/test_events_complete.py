"""All event types, environmental rearming, merge/cap boundaries, and no-stage data."""
from datetime import datetime, timedelta, timezone
from dataclasses import replace
from zoneinfo import ZoneInfo

import pytest

from backend.domain.events import Event_Type, MAX_EVENT_COUNT
from backend.domain.session import SleepSession
from backend.domain.stages import Sleep_Stage, Stage_Segment
from backend.domain.telemetry import Metric, TelemetryPoint, CANONICAL_UNIT
from backend.processing.aligner import align
from backend.processing.events import (
    Event_Candidate, detect, detect_environmental_events, detect_movement_events, merge_and_cap,
    smoothed_samples,
)
from backend.processing.features import extract, smooth_coarse_states

T0 = datetime(2024, 3, 1, 22, tzinfo=timezone.utc)


def environment_session():
    end = T0 + timedelta(hours=4)
    points = []
    for minute in range(240):
        raised = 60 <= minute < 120 or 180 <= minute
        for metric, baseline, change in ((Metric.temperature, 20, 3), (Metric.humidity, 40, 15), (Metric.pressure, 1010, 3)):
            points.append(TelemetryPoint(T0 + timedelta(minutes=minute), "room", metric,
                                        baseline + change * raised, CANONICAL_UNIT[metric]))
        points.append(TelemetryPoint(T0 + timedelta(minutes=minute), "wearable", Metric.steps,
                                    24 if 20 <= minute < 40 else 0, "steps/min"))
    return SleepSession(T0, end, "manual", telemetry=tuple(points))


def test_each_environmental_type_and_rearming():
    session = environment_session()
    timeline, _ = align(session)
    candidates = detect_environmental_events(timeline)
    expected = {Event_Type.temperature_rising, Event_Type.temperature_falling,
                Event_Type.humidity_rising, Event_Type.humidity_falling,
                Event_Type.pressure_rising, Event_Type.pressure_falling}
    assert {e.type for e in candidates} == expected
    for metric in (Metric.temperature, Metric.humidity, Metric.pressure):
        typed = [e for e in candidates if e.type.value.startswith(metric.value)]
        assert len(typed) >= 3
        assert typed[0].type.value.endswith("rising")
        assert any(e.type.value.endswith("falling") for e in typed)
        assert all(.5 <= e.magnitude <= 1 for e in typed)


def test_movement_restless_magnitudes_labels_and_no_stage_session():
    session = environment_session()
    timeline, _ = align(session)
    features, states, _ = extract(timeline, session, 120)
    movement = detect_movement_events(features)
    assert [(e.type, e.night_time) for e in movement] == [
        (Event_Type.movement, T0 + timedelta(minutes=20)),
        (Event_Type.restless_period, T0 + timedelta(minutes=20)),
    ]
    assert all(e.magnitude == pytest.approx(.8) for e in movement)
    events = detect(session, timeline, features, states, 120, ZoneInfo("America/New_York"))
    assert not any(e.type in (Event_Type.sleep_onset, Event_Type.awake, Event_Type.awakening,
                              Event_Type.deep_sleep, Event_Type.light_sleep, Event_Type.rem) for e in events)
    assert next(e for e in events if e.type is Event_Type.restless_period).label == "17:20 restless period"


def test_environment_missing_samples_cannot_emit_or_rearm():
    session = environment_session()
    timeline, _ = align(session)
    metric = Metric.temperature
    # One non-missing sample has no eligible comparison pair, even though its smoothing is defined.
    values, masks = dict(timeline.values), dict(timeline.missing)
    values[metric] = tuple(20 if i == 120 else None for i in range(timeline.sample_count))
    masks[metric] = tuple(i != 120 for i in range(timeline.sample_count))
    timeline = replace(timeline, values={metric: values[metric]}, missing={metric: masks[metric]})
    assert smoothed_samples(timeline, metric)[120] == 20
    assert sum(v is not None for v in smoothed_samples(timeline, metric)) == 1
    assert detect_environmental_events(timeline) == []


def test_environment_smoothed_values_are_centered_and_truncated():
    session = environment_session()
    timeline, _ = align(session)
    metric = Metric.temperature
    values = smoothed_samples(timeline, metric)
    for index in (0, 59, 60, 120, 239):
        t = timeline.timestamps[index]
        measured = [v for instant, v in zip(timeline.timestamps, timeline.values[metric])
                    if abs(instant - t) <= timedelta(minutes=7.5) and v is not None]
        assert values[index] == pytest.approx(sum(measured) / len(measured))


def test_merge_is_inclusive_and_anchored_to_earlier_retained_event():
    events = [Event_Candidate(Event_Type.movement, T0 + timedelta(minutes=t), m)
              for t, m in ((0, .1), (15, .9), (20, .4), (30, .7))]
    got = merge_and_cap(events)
    assert [(e.night_time, e.magnitude) for e in got] == [
        (T0, .9), (T0 + timedelta(minutes=20), .7)]


def test_cap_keeps_onset_awakening_and_earlier_magnitude_ties():
    onset = Event_Candidate(Event_Type.sleep_onset, T0, 1)
    wake = Event_Candidate(Event_Type.awakening, T0 + timedelta(hours=8), 1)
    others = [Event_Candidate(Event_Type.movement, T0 + timedelta(minutes=16 * i), .5)
              for i in range(1, 20)]
    got = merge_and_cap([wake, *reversed(others), onset])
    assert len(got) == MAX_EVENT_COUNT
    assert onset in got and wake in got
    assert [e for e in got if e.type is Event_Type.movement] == others[:10]


def test_coarse_state_short_first_and_recombined_neighbors():
    assert smooth_coarse_states([Sleep_Stage.awake, Sleep_Stage.deep, Sleep_Stage.deep,
                                 Sleep_Stage.rem, Sleep_Stage.deep, Sleep_Stage.deep]) == [Sleep_Stage.deep] * 6
    assert smooth_coarse_states([Sleep_Stage.awake, Sleep_Stage.awake, Sleep_Stage.rem,
                                 Sleep_Stage.deep, Sleep_Stage.deep]) == [Sleep_Stage.awake] * 3 + [Sleep_Stage.deep] * 2


def test_empty_detection_is_valid():
    session = SleepSession(T0, T0 + timedelta(hours=1), "manual")
    timeline, _ = align(session)
    features, states, _ = extract(timeline, session, 30)
    assert detect(session, timeline, features, states, 30, ZoneInfo("UTC")) == []


def test_all_stage_transition_types_and_duration_magnitudes():
    cursor, segments = T0, []
    for stage, minutes in ((Sleep_Stage.awake, 10), (Sleep_Stage.light, 30), (Sleep_Stage.deep, 15),
                           (Sleep_Stage.awake, 6), (Sleep_Stage.light, 12), (Sleep_Stage.rem, 25), (Sleep_Stage.awake, 20)):
        end = cursor + timedelta(minutes=minutes)
        segments.append(Stage_Segment(cursor, end, stage))
        cursor = end
    session = SleepSession(T0, cursor, "fitbit", tuple(segments))
    timeline, _ = align(session)
    features, states, _ = extract(timeline, session, 30)
    events = detect(session, timeline, features, states, 30, ZoneInfo("UTC"))
    assert {e.type: e.magnitude for e in events} == {
        Event_Type.sleep_onset: 1, Event_Type.deep_sleep: .75, Event_Type.awake: .6,
        Event_Type.light_sleep: .6, Event_Type.rem: 1, Event_Type.awakening: 1,
    }
