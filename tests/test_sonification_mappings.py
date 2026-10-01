import inspect
import random
from dataclasses import replace
from datetime import timedelta

import pytest
from hypothesis import given, settings, strategies as st

from backend.domain.events import Event_Type, Night_Event
from backend.domain.mapping import DEFAULT_MAPPING, METRIC_KEYS, Metric_Mapping
from backend.domain.sound import Breakpoint, Mapping_Target, PARAMETER_RATE_LIMITS, Preset_Span, Transient_Event
from backend.domain.stages import Sleep_Stage
from backend.sonification.combine import combine, rate_limit
from backend.sonification.contributions import Hysteresis_Reference, contribution_trajectory, mapped_value
from backend.sonification.engine import build_render_plan
from backend.sonification.gestures import schedule_gestures
from backend.sonification.normalize import Normalization_Span, normalization_span, normalization_spans
from backend.sonification.presets import STATE_PRESETS, preset_gains, preset_plan
from backend.sonification.transients import cap_transients, schedule_transients
from sonification_helpers import features

UNIT = st.floats(0, 1, allow_nan=False)


# Feature: sleep-replay-sonification, Property 3: Physiological contribution monotonicity
@settings(max_examples=100)
@given(st.sampled_from(("heart_rate", "hrv")), UNIT, UNIT, UNIT)
def test_physiological_monotonicity(key, a, b, sensitivity):
    a, b = sorted((a, b))
    assert mapped_value(a, sensitivity, key) <= mapped_value(b, sensitivity, key)


# Feature: sleep-replay-sonification, Property 8: Environmental normalization linearity
@settings(max_examples=100)
@given(st.floats(-20, 100), st.floats(2, 30), UNIT, UNIT)
def test_normalization_linearity(midpoint, width, a, b):
    span = Normalization_Span(midpoint - width / 2, midpoint + width / 2)
    assert span.normalize(span.low) == 0
    assert span.normalize(span.high) == 1
    v1, v2 = span.low + a * width, span.low + b * width
    assert span.normalize(v2) - span.normalize(v1) == pytest.approx((v2 - v1) / width, abs=1e-12)


# Feature: sleep-replay-sonification, Property 9: Environmental contribution monotonicity
@settings(max_examples=100)
@given(st.sampled_from(("temperature", "humidity", "pressure")), UNIT, UNIT, UNIT)
def test_environmental_monotonicity(key, a, b, sensitivity):
    a, b = sorted((a, b))
    assert mapped_value(a, sensitivity, key) <= mapped_value(b, sensitivity, key)


# Feature: sleep-replay-sonification, Property 10: Pressure contribution cap
@settings(max_examples=100)
@given(st.floats(-1e6, 1e6), UNIT)
def test_pressure_cap(normalized, sensitivity):
    assert abs(mapped_value(normalized, sensitivity, "pressure") - 0.5) <= 0.25


# Feature: sleep-replay-sonification, Property 11: Hysteresis hold and update
@settings(max_examples=100)
@given(st.lists(st.floats(0, 100), min_size=1, max_size=30), st.floats(0, 10))
def test_hysteresis_hold_and_update(values, threshold):
    reference = Hysteresis_Reference(threshold)
    last, held = None, 0.5
    for v in values:
        expected = mapped_value(v / 100, 1, "heart_rate")
        if last is None or abs(v - last) >= threshold:
            last, held = v, expected
        assert reference.update(v, expected) == held
        assert reference.reference == last


# Feature: sleep-replay-sonification, Property 5: Missing-feature glide behavior
@settings(max_examples=100)
@given(st.sampled_from(("heart_rate", "hrv", "temperature", "humidity", "pressure")),
       st.lists(st.booleans(), min_size=2, max_size=20), UNIT)
def test_glide_behavior(key, missing, value):
    series = features([None if m else value for m in missing], key=key, window_s=3)
    mapping = Metric_Mapping(Mapping_Target.intensity, 1, 1, 0)
    trajectory = contribution_trajectory(series, key, mapping, Normalization_Span(0, 1), len(missing) * 3)
    target = mapped_value(value, 1, key)
    assert trajectory.value_at(0) == (0.5 if missing[0] else target)
    for i in range(1, len(missing)):
        if missing[i] != missing[i - 1]:
            start = i * 3
            before = target if missing[i] else 0.5
            after = 0.5 if missing[i] else target
            assert trajectory.value_at(start) == pytest.approx(before)
            assert trajectory.value_at(start + 0.5) == pytest.approx((before + after) / 2)
            assert trajectory.value_at(start + 1) == pytest.approx(after)
            assert trajectory.value_at(start + 2.9) == pytest.approx(after)


# Feature: sleep-replay-sonification, Property 14: Sensitivity monotonicity
@settings(max_examples=100)
@given(st.sampled_from(METRIC_KEYS), UNIT, UNIT, UNIT)
def test_sensitivity_monotonicity(key, value, a, b):
    a, b = sorted((a, b))
    assert abs(mapped_value(value, a, key) - 0.5) <= abs(mapped_value(value, b, key) - 0.5) + 1e-15


# Feature: sleep-replay-sonification, Property 12: Sound_Parameter rate limit
@settings(max_examples=100)
@given(st.lists(UNIT, min_size=2, max_size=25), st.sampled_from((0.2, 0.5)), UNIT, UNIT)
def test_rate_limit(values, rate, a, b):
    trajectory = rate_limit([Breakpoint(i * 0.5, v) for i, v in enumerate(values)], rate)
    t1, t2 = sorted((a * (len(values) - 1) * 0.5, b * (len(values) - 1) * 0.5))
    assert abs(trajectory.value_at(t2) - trajectory.value_at(t1)) <= rate * (t2 - t1) + 1e-14
    for p, q in zip(trajectory.breakpoints, trajectory.breakpoints[1:]):
        assert abs(q.value - p.value) <= rate * (q.t - p.t) + 1e-14


# Feature: sleep-replay-sonification, Property 13: Sound_Parameter bounds invariant
@settings(max_examples=100)
@given(st.lists(st.one_of(st.none(), st.floats(30, 120)), min_size=60, max_size=60), UNIT, UNIT)
def test_engine_bounds(values, sensitivity, t):
    series = features(values, movement=1)
    metrics = dict(DEFAULT_MAPPING.metrics, heart_rate=replace(DEFAULT_MAPPING.metrics["heart_rate"], sensitivity=sensitivity))
    plan = build_render_plan(series, series.dominant_stages, (), replace(DEFAULT_MAPPING, metrics=metrics, target_duration=30), 30, 42)
    for parameter, trajectory in plan.trajectories.items():
        assert 0 <= trajectory.value_at(t * 30) <= 1
        for a, b in zip(trajectory.breakpoints, trajectory.breakpoints[1:]):
            assert abs(a.value - b.value) <= PARAMETER_RATE_LIMITS[parameter] * (b.t - a.t) + 1e-12


# Feature: sleep-replay-sonification, Property 15: Sonification from features only
@settings(max_examples=100)
@given(st.lists(st.one_of(st.none(), st.floats(40, 100)), min_size=60, max_size=60), st.integers(0, 2**32 - 1))
def test_features_only_determinism(values, seed):
    series = features(values)
    # The seam exposes no session, timeline, raw points, I/O or clock parameter.
    assert tuple(inspect.signature(build_render_plan).parameters) == ("features", "coarse_states", "night_events", "config", "target_duration", "seed")
    first = build_render_plan(series, series.dominant_stages, (), DEFAULT_MAPPING, 30, seed)
    random.random()  # unrelated process randomness cannot change the result
    second = build_render_plan(replace(series), tuple(series.dominant_stages), (), DEFAULT_MAPPING, 30, seed)
    assert first == second


# Feature: sleep-replay-sonification, Property 6: Movement transient amplitude monotonicity
@settings(max_examples=100)
@given(st.floats(0.101, 1), st.floats(0.001, 1), st.floats(0.001, 1))
def test_transient_monotonicity(intensity, a, b):
    a, b = sorted((a, b))
    series = features([intensity], key="movement")
    def scheduled(sensitivity):
        config = replace(DEFAULT_MAPPING, metrics=dict(DEFAULT_MAPPING.metrics, movement=Metric_Mapping(Mapping_Target.intensity, sensitivity)))
        return schedule_transients(series, config, random.Random(1))[0]
    first, second = scheduled(a), scheduled(b)
    assert 0 < first.amplitude <= second.amplitude <= 1
    assert first.onset_s == second.onset_s
    assert 0 <= first.onset_s < 0.5


# Feature: sleep-replay-sonification, Property 7: Transient density cap
@settings(max_examples=100)
@given(st.lists(st.tuples(st.floats(0, 10), st.floats(0.001, 1)), max_size=40))
def test_density_cap(values):
    candidates = [Transient_Event(t, a, "movement") for t, a in values]
    accepted = cap_transients(candidates)
    for first, fifth in zip(accepted, accepted[4:]):
        assert fifth.onset_s - first.onset_s >= 1
    assert set(accepted) <= set(candidates)


def test_weighted_combination_neutral_and_disabled():
    series = features([0.2, 0.8], window_s=15)
    spans = normalization_spans(series)
    config = replace(DEFAULT_MAPPING, metrics={k: replace(m, target=Mapping_Target.intensity) for k, m in DEFAULT_MAPPING.metrics.items()})
    contributions = {k: contribution_trajectory(series, k, m, spans.get(k), 30) for k, m in config.metrics.items()}
    combined = combine(contributions, config, series, [False, False], 30)
    expected = sum(contributions[k].value_at(0) * m.sensitivity for k, m in config.metrics.items()) / sum(m.sensitivity for m in config.metrics.values())
    assert combined[Mapping_Target.intensity].value_at(0) == expected
    assert combined[Mapping_Target.brightness].value_at(0) == 0.5
    for m in (Metric_Mapping(Mapping_Target.none, 1, 1, 0), Metric_Mapping(Mapping_Target.intensity, 0, 1, 0)):
        assert contribution_trajectory(series, "heart_rate", m, spans["heart_rate"], 30).value_at(10) == 0.5


def test_mid_glide_return_restarts_from_current_value():
    series = features([1, None, 0, None, 1], window_s=0.5)
    trajectory = contribution_trajectory(series, "heart_rate", Metric_Mapping(Mapping_Target.intensity, 1, 1, 0), Normalization_Span(0, 1), 2.5)
    assert trajectory.value_at(0.5) == 1
    assert trajectory.value_at(1) == 0.75
    assert trajectory.value_at(1.5) == 0.375
    assert trajectory.value_at(2) == 0.4375


def test_session_hrv_and_unavailable_metrics():
    series = features([None] * 60, key="hrv")
    mapping = DEFAULT_MAPPING.metrics["hrv"]
    low = contribution_trajectory(replace(series, session_hrv=20), "hrv", mapping, None, 30)
    high = contribution_trajectory(replace(series, session_hrv=80), "hrv", mapping, None, 30)
    assert low.value_at(10) < high.value_at(10)
    assert low.value_at(0) == low.value_at(30)
    for key in METRIC_KEYS:
        assert contribution_trajectory(replace(series, unavailable_metrics=(key,)), key, DEFAULT_MAPPING.metrics[key], None, 30).value_at(20) == 0.5


def test_presets_crossfades_and_restless_boost_once():
    for state in Sleep_Stage:
        series = features([50] * 60, state=state)
        assert preset_plan(series, series.dominant_stages)[0].preset == STATE_PRESETS[state.value]
    series = features([50] * 60)
    states = (Sleep_Stage.light, Sleep_Stage.deep, Sleep_Stage.deep, *([Sleep_Stage.light] * 57))
    spans = preset_plan(series, states)
    assert spans[1].crossfade_in_s == 0.5
    assert spans[2].crossfade_in_s == 1.0
    before, middle, after = [preset_gains(spans, t) for t in (0.25, 0.5, 0.75)]
    assert before["Light"] == 1 and middle["Light"] == 0.5 and after["Light"] == 0
    assert before["Deep"] == 0 and middle["Deep"] == 0.5 and after["Deep"] == 1
    restless = features([50] * 60, state=Sleep_Stage.restless)
    plan = build_render_plan(restless, restless.dominant_stages, (), DEFAULT_MAPPING, 30, 1)
    assert plan.trajectories[Mapping_Target.texture_density].value_at(10) == pytest.approx(0.5 + 0.25 * 0.8)
    disabled = replace(DEFAULT_MAPPING, metrics=dict(DEFAULT_MAPPING.metrics, movement=Metric_Mapping(Mapping_Target.none, 1)))
    plan = build_render_plan(restless, restless.dominant_stages, (), disabled, 30, 1)
    assert plan.trajectories[Mapping_Target.texture_density].value_at(10) == 0.5


def test_transient_threshold_brief_awakening_and_cap_ties():
    assert not schedule_transients(features([0.1], key="movement"), DEFAULT_MAPPING, random.Random(1))
    series = features([0.9] * 60, key="movement")
    start = series.windows[0].start_time + timedelta(seconds=100)
    series = replace(series, brief_awakenings=((start, start + timedelta(seconds=20)),))
    for mapping in (Metric_Mapping(Mapping_Target.none, 1), Metric_Mapping(Mapping_Target.intensity, 0)):
        config = replace(DEFAULT_MAPPING, metrics=dict(DEFAULT_MAPPING.metrics, movement=mapping))
        events = schedule_transients(series, config, random.Random(1))
        assert len(events) == 1 and events[0].kind == "brief_awakening"
        assert events[0].onset_s == 100 / series.compression_ratio
    candidates = [Transient_Event(t / 10, 0.5, "movement") for t in range(8)]
    assert cap_transients(candidates) == tuple(candidates[:4])
    assert cap_transients([*candidates, Transient_Event(0.8, 0.9, "brief_awakening")])[-1].amplitude == 0.9


def test_gestures_inward_clamping_and_no_transients():
    series = features([50] * 60)
    events = tuple(Night_Event(kind, series.windows[0].start_time, t, 1, "00:00 " + kind.value)
                   for kind, t in ((Event_Type.sleep_onset, 0), (Event_Type.awakening, 30)))
    gestures = schedule_gestures(events, 30)
    assert [(g.start_s, g.end_s) for g in gestures] == [(0, 2), (28, 30)]
    assert not build_render_plan(series, series.dominant_stages, events, DEFAULT_MAPPING, 30, 1).transients
