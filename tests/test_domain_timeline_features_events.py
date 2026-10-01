"""Unit tests for backend.domain.timeline, features, and events (task 2.7).

Requirements 10.6 / 10.15 (sample timestamps and count), 12.1–12.12 (feature
types and defaults), 13.11 / 13.12 (Night_Event and Event_Vocabulary order),
and the Default Parameter Values table.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from backend.domain import events as ev
from backend.domain.events import EVENT_VOCABULARY_ORDER, Event_Type, Night_Event
from backend.domain.features import (
    FEATURE_WINDOW_SPAN_S,
    MINIMUM_STATE_DURATION_S,
    SMOOTHING_WINDOW,
    Coarse_State,
    Feature_Series,
    Feature_Window,
    Metric_Window_Features,
    feature_window_count,
)
from backend.domain.stages import Sleep_Stage as S
from backend.domain.telemetry import CONTINUOUS_METRICS, Metric
from backend.domain.timeline import (
    FALLBACK_TIMELINE_RESOLUTION_S,
    MAX_INTERPOLATION_GAP,
    STEPS_VALUE_SPAN,
    TIMELINE_RESOLUTION_CANDIDATES_S,
    Aligned_Timeline,
    sample_count,
    sample_timestamps,
)

UTC = timezone.utc
START = datetime(2024, 3, 1, 23, 0, tzinfo=timezone(timedelta(hours=-5)))


def after(seconds: float) -> datetime:
    return START + timedelta(seconds=seconds)


# =============================================================================
# Timeline
# =============================================================================


def test_timeline_defaults():
    assert TIMELINE_RESOLUTION_CANDIDATES_S == (1, 5, 10, 15, 30, 60)
    assert FALLBACK_TIMELINE_RESOLUTION_S == 60
    assert set(MAX_INTERPOLATION_GAP) == CONTINUOUS_METRICS
    assert MAX_INTERPOLATION_GAP[Metric.heart_rate] == timedelta(minutes=5)
    for metric in (Metric.hrv_rmssd, Metric.temperature, Metric.humidity, Metric.pressure):
        assert MAX_INTERPOLATION_GAP[metric] == timedelta(minutes=15)
    assert STEPS_VALUE_SPAN == timedelta(seconds=60)


@pytest.mark.parametrize(
    ("duration_s", "resolution_s", "expected"),
    [
        (8 * 3600, 30, 960),
        (8 * 3600, 1, 28800),
        (60, 60, 1),  # exact multiple
        (61, 60, 2),  # rounds up
        (0.000001, 60, 1),  # one microsecond
        (7 * 3600 + 1, 15, 1681),
    ],
)
def test_sample_count_is_ceiling_of_duration(duration_s, resolution_s, expected):
    assert sample_count(START, after(duration_s), resolution_s) == expected


def test_sample_count_uses_elapsed_time_across_dst():
    ny = ZoneInfo("America/New_York")
    # Spring forward: 01:00 EST to 04:00 EDT is 2 hours of elapsed time.
    spring = sample_count(datetime(2024, 3, 10, 1, 0, tzinfo=ny), datetime(2024, 3, 10, 4, 0, tzinfo=ny), 60)
    assert spring == 120
    # Fall back: 00:00 EDT to 03:00 EST is 4 hours of elapsed time.
    fall = sample_count(datetime(2024, 11, 3, 0, 0, tzinfo=ny), datetime(2024, 11, 3, 3, 0, tzinfo=ny), 60)
    assert fall == 240


@pytest.mark.parametrize("resolution_s", [0, 2, 7, 120, 30.5, True])
def test_sample_count_rejects_non_candidate_resolution(resolution_s):
    with pytest.raises(ValueError):
        sample_count(START, after(600), resolution_s)


def test_sample_count_rejects_naive_and_empty_ranges():
    with pytest.raises(ValueError):
        sample_count(datetime(2024, 3, 1, 23), after(600), 60)
    with pytest.raises(ValueError):
        sample_count(START, START, 60)
    with pytest.raises(ValueError):
        sample_count(after(10), START, 60)


def test_sample_timestamps_are_utc_multiples_before_end():
    stamps = sample_timestamps(START, after(125), 60)
    assert stamps == (after(0), after(60), after(120))
    assert all(s.tzinfo is UTC for s in stamps)
    assert stamps[-1] < after(125)


def _timeline(duration_s: float = 150, resolution_s: int = 60, **overrides) -> Aligned_Timeline:
    n = sample_count(START, after(duration_s), resolution_s)
    fields = dict(
        start_time=START,
        end_time=after(duration_s),
        resolution_s=resolution_s,
        timestamps=sample_timestamps(START, after(duration_s), resolution_s),
        values={Metric.heart_rate: [60.0] * (n - 1) + [None]},
        missing={Metric.heart_rate: [False] * (n - 1) + [True]},
        stages=[S.light] * n,
    )
    fields.update(overrides)
    return Aligned_Timeline(**fields)


def test_aligned_timeline_valid_construction_and_accessors():
    tl = _timeline()
    assert tl.sample_count == 3
    assert tl.duration == timedelta(seconds=150)
    assert isinstance(tl.stages, tuple)
    assert tl.values[Metric.heart_rate] == (60.0, 60.0, None)
    assert tl.sample_interval(0) == (after(0), after(60))
    assert tl.sample_interval(2) == (after(120), after(150))  # last ends at end_time
    assert tl.sample_interval(-1) == (after(120), after(150))
    assert tl.is_missing(Metric.heart_rate, 2) is True
    assert tl.is_missing(Metric.heart_rate, 0) is False
    assert tl.is_missing(Metric.pressure, 0) is True  # metric absent from timeline


def test_aligned_timeline_accepts_timestamps_in_other_offsets():
    stamps = tuple(s.astimezone(START.tzinfo) for s in sample_timestamps(START, after(150), 60))
    assert _timeline(timestamps=stamps).sample_count == 3


@pytest.mark.parametrize(
    ("overrides", "error"),
    [
        ({"timestamps": sample_timestamps(START, after(90), 60)}, ValueError),  # wrong count
        ({"timestamps": (after(0), after(61), after(120))}, ValueError),  # not a multiple
        ({"values": {Metric.heart_rate: [60.0, 60.0, 61.0]}}, ValueError),  # missing sample with value
        ({"values": {Metric.heart_rate: [60.0, None, None]}}, ValueError),  # present sample is None
        ({"values": {Metric.heart_rate: [60.0, math.nan, None]}}, ValueError),
        ({"values": {Metric.heart_rate: [60.0, True, None]}}, ValueError),
        ({"values": {Metric.heart_rate: [60.0, None]}, "missing": {Metric.heart_rate: [False, True]}}, ValueError),
        ({"missing": {Metric.heart_rate: [0, 0, 1]}}, TypeError),  # mask must be bool
        ({"values": {Metric.steps: [1.0, 1.0, None]}}, ValueError),  # key mismatch
        ({"values": {"heart_rate": [60.0, 60.0, None]}, "missing": {"heart_rate": [False, False, True]}}, TypeError),
        ({"stages": [S.light] * 2}, ValueError),
        ({"stages": ["light"] * 3}, TypeError),
        ({"resolution_s": 45}, ValueError),
    ],
)
def test_aligned_timeline_validation(overrides, error):
    with pytest.raises(error):
        _timeline(**overrides)


def test_aligned_timeline_copies_input_mappings():
    values = {Metric.heart_rate: [60.0, 60.0, None]}
    tl = _timeline(values=values)
    values[Metric.heart_rate][0] = 99.0
    assert tl.values[Metric.heart_rate][0] == 60.0


# =============================================================================
# Features
# =============================================================================


def test_feature_defaults():
    assert FEATURE_WINDOW_SPAN_S == 0.5
    assert MINIMUM_STATE_DURATION_S == 1.0
    assert set(SMOOTHING_WINDOW) == CONTINUOUS_METRICS
    assert SMOOTHING_WINDOW[Metric.heart_rate] == timedelta(minutes=5)
    assert SMOOTHING_WINDOW[Metric.pressure] == timedelta(minutes=15)
    assert Coarse_State is S


@pytest.mark.parametrize(
    ("target_s", "expected"),
    [(30, 60), (120, 240), (180, 360), (300, 600), (600, 1200), (0.75, 2), (0.1, 1)],
)
def test_feature_window_count(target_s, expected):
    assert feature_window_count(target_s) == expected


@pytest.mark.parametrize(("target_s", "span_s"), [(0, 0.5), (-30, 0.5), (30, 0), (math.nan, 0.5)])
def test_feature_window_count_rejects_non_positive(target_s, span_s):
    with pytest.raises(ValueError):
        feature_window_count(target_s, span_s)


def mwf(**overrides) -> Metric_Window_Features:
    fields = dict(mean=60.0, minimum=55.0, maximum=65.0, slope=-1.5, missing_fraction=0.25, smoothed=59.0)
    fields.update(overrides)
    return Metric_Window_Features(**fields)


def test_metric_window_features_present_and_missing():
    assert mwf().has_values
    missing = mwf(mean=None, minimum=None, maximum=None, slope=None, missing_fraction=1.0, smoothed=58.0)
    assert not missing.has_values
    assert missing.smoothed == 58.0  # smoothing is independent of the window's own samples
    assert mwf(minimum=60.0, maximum=60.0, missing_fraction=0.0, slope=None).has_values


@pytest.mark.parametrize(
    "overrides",
    [
        {"mean": None},  # partial presence
        {"maximum": None},
        {"mean": 70.0},  # mean above maximum
        {"mean": 50.0},  # mean below minimum
        {"missing_fraction": 1.5},
        {"missing_fraction": -0.1},
        {"slope": math.inf},
        {"smoothed": math.nan},
        {"mean": None, "minimum": None, "maximum": None, "slope": None, "missing_fraction": 0.5},
        {"mean": None, "minimum": None, "maximum": None, "slope": 1.0, "missing_fraction": 1.0},
    ],
)
def test_metric_window_features_validation(overrides):
    with pytest.raises(ValueError):
        mwf(**overrides)


def window(start_s: float = 0, end_s: float = 30, **overrides) -> Feature_Window:
    fields = dict(
        start_time=after(start_s),
        end_time=after(end_s),
        per_metric={Metric.heart_rate: mwf()},
        stage_fractions={S.light: 0.75, S.awake: 0.25},
        dominant_stage=S.light,
        movement_intensity=0.2,
    )
    fields.update(overrides)
    return Feature_Window(**fields)


def test_feature_window_accessors():
    w = window(0, 30)
    assert w.midpoint == after(15)
    assert w.stage_fraction(S.light) == 0.75
    assert w.stage_fraction(S.deep) == 0.0
    # Fractions within tolerance of 1.0 are accepted.
    window(stage_fractions={S.light: 0.3333333, S.rem: 0.6666666})


@pytest.mark.parametrize(
    ("overrides", "error"),
    [
        ({"end_time": after(0)}, ValueError),
        ({"start_time": datetime(2024, 3, 1, 23)}, ValueError),
        ({"per_metric": {Metric.steps: mwf()}}, ValueError),  # steps is not continuous
        ({"per_metric": {Metric.heart_rate: {"mean": 60.0}}}, TypeError),
        ({"stage_fractions": {S.light: 0.5, S.awake: 0.25}}, ValueError),  # sum 0.75
        ({"stage_fractions": {S.light: 1.25, S.awake: -0.25}}, ValueError),
        ({"stage_fractions": {"light": 1.0}}, TypeError),
        ({"dominant_stage": "light"}, TypeError),
        ({"movement_intensity": 1.01}, ValueError),
        ({"movement_intensity": -0.01}, ValueError),
    ],
)
def test_feature_window_validation(overrides, error):
    with pytest.raises(error):
        window(**overrides)


def test_feature_series_contiguity_and_accessors():
    windows = [window(0, 30), window(30, 60, dominant_stage=S.awake, stage_fractions={S.awake: 1.0})]
    series = Feature_Series(windows, compression_ratio=960.0)
    assert len(series) == 2
    assert isinstance(series.windows, tuple)
    assert series.dominant_stages == (S.light, S.awake)
    assert len(Feature_Series((), compression_ratio=1.0)) == 0


@pytest.mark.parametrize(
    ("windows", "ratio", "error"),
    [
        ([window(0, 30), window(31, 60)], 960.0, ValueError),  # gap
        ([window(0, 30), window(20, 60)], 960.0, ValueError),  # overlap
        ([window(0, 30)], 0.0, ValueError),
        ([window(0, 30)], math.inf, ValueError),
        (["not a window"], 1.0, TypeError),
    ],
)
def test_feature_series_validation(windows, ratio, error):
    with pytest.raises(error):
        Feature_Series(windows, compression_ratio=ratio)


# =============================================================================
# Events
# =============================================================================


def test_event_vocabulary_order_matches_glossary():
    assert [e.value for e in EVENT_VOCABULARY_ORDER] == [
        "sleep onset", "awake", "awakening", "light sleep", "deep sleep", "REM",
        "movement", "restless period", "temperature rising", "temperature falling",
        "humidity rising", "humidity falling", "pressure rising", "pressure falling",
    ]
    assert set(EVENT_VOCABULARY_ORDER) == set(Event_Type)
    assert [ev.EVENT_VOCABULARY_RANK[e] for e in EVENT_VOCABULARY_ORDER] == list(range(14))
    assert Event_Type.rem.description == "REM"


def test_event_type_lookup_tables():
    assert ev.STAGE_TRANSITION_EVENT_TYPE == {
        S.light: Event_Type.light_sleep, S.deep: Event_Type.deep_sleep, S.rem: Event_Type.rem,
    }
    assert ev.ENVIRONMENTAL_METRICS == (Metric.temperature, Metric.humidity, Metric.pressure)
    for metric in ev.ENVIRONMENTAL_METRICS:
        assert ev.ENVIRONMENTAL_EVENT_TYPE[(metric, True)].value == f"{metric.value} rising"
        assert ev.ENVIRONMENTAL_EVENT_TYPE[(metric, False)].value == f"{metric.value} falling"


def test_event_detection_defaults():
    assert ev.WAKE_EVENT_MIN_DURATION == timedelta(minutes=5)
    assert ev.MAJOR_TRANSITION_MIN_DURATION == timedelta(minutes=10)
    assert ev.MOVEMENT_TRANSIENT_THRESHOLD == 0.1
    assert ev.MOVEMENT_BURST_THRESHOLD == 0.6
    assert ev.RESTLESS_THRESHOLD == 0.3
    assert ev.RESTLESS_MIN_DURATION == timedelta(minutes=10)
    assert ev.ENVIRONMENTAL_CHANGE_THRESHOLD == {
        Metric.temperature: 1.0, Metric.humidity: 5.0, Metric.pressure: 1.0,
    }
    assert ev.ENVIRONMENTAL_CHANGE_SPAN == {
        Metric.temperature: timedelta(minutes=30),
        Metric.humidity: timedelta(minutes=30),
        Metric.pressure: timedelta(hours=3),
    }
    assert ev.EVENT_MERGE_WINDOW == timedelta(minutes=15)
    assert ev.MAX_EVENT_COUNT == 12


def event(**overrides) -> Night_Event:
    fields = dict(
        type=Event_Type.restless_period,
        night_time=datetime(2024, 3, 2, 2, 17, 40, tzinfo=START.tzinfo),
        replay_time_s=12.5,
        magnitude=0.4,
        label="02:17 restless period",
    )
    fields.update(overrides)
    return Night_Event(**fields)


def test_night_event_valid():
    e = event()
    assert e.description == "restless period"
    assert event(type=Event_Type.rem, label="00:00 REM", magnitude=0.0, replay_time_s=0).magnitude == 0.0
    assert event(type=Event_Type.sleep_onset, label="23:59 sleep onset", magnitude=1.0).magnitude == 1.0


@pytest.mark.parametrize(
    ("overrides", "error"),
    [
        ({"type": "restless period"}, TypeError),
        ({"night_time": datetime(2024, 3, 2, 2, 17)}, ValueError),
        ({"replay_time_s": -0.1}, ValueError),
        ({"replay_time_s": math.nan}, ValueError),
        ({"magnitude": 1.01}, ValueError),
        ({"magnitude": -0.01}, ValueError),
        ({"magnitude": True}, ValueError),
        ({"label": None}, TypeError),
        ({"label": "2:17 restless period"}, ValueError),
        ({"label": "24:00 restless period"}, ValueError),
        ({"label": "02:60 restless period"}, ValueError),
        ({"label": "02:17 Restless period"}, ValueError),
        ({"label": "02:17 movement"}, ValueError),
        ({"label": "02:17  restless period"}, ValueError),
        ({"label": "02:17"}, ValueError),
    ],
)
def test_night_event_validation(overrides, error):
    with pytest.raises(error):
        event(**overrides)


def test_night_event_sort_key_orders_by_instant_then_vocabulary():
    same_instant_other_offset = START.astimezone(UTC)
    a = event(type=Event_Type.awake, night_time=START, label="23:00 awake")
    b = event(type=Event_Type.sleep_onset, night_time=same_instant_other_offset, label="04:00 sleep onset")
    c = event(type=Event_Type.movement, night_time=after(-60), label="22:59 movement")
    assert sorted([a, b, c], key=lambda e: e.sort_key) == [c, b, a]
