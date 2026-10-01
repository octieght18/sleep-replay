"""Sanity tests for stage fractions and Movement_Intensity (task 13.2, Req 12.6-12.8, 12.11)."""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import pytest

from backend.domain.errors import Processing_Report
from backend.domain.features import STAGE_FRACTION_TOLERANCE
from backend.domain.session import SleepSession
from backend.domain.stages import DOMINANT_STAGE_ORDER, Sleep_Stage, Stage_Segment
from backend.domain.telemetry import Metric
from backend.domain.timeline import Aligned_Timeline, sample_timestamps
from backend.processing.compression import night_compression
from backend.processing.features import (
    MOVEMENT_FULL_SCALE,
    STAGE_DERIVED_MOVEMENT_WARNING,
    assemble_feature_series,
    dominant_stage,
    feature_window_bounds,
    metric_window_features,
    movement_intensity_from_mean,
    window_mean_steps,
    window_movement_intensity,
    window_stage_coverage,
)

S = Sleep_Stage
START = datetime(2024, 3, 1, 22, 0, tzinfo=timezone.utc)
END = START + timedelta(hours=2)
RES = 10  # 720 samples; Target 30 s -> 60 windows of 120 s (12 samples each)


def _at(seconds: float) -> datetime:
    return START + timedelta(seconds=seconds)


def _seg(a: float, b: float, stage: Sleep_Stage, brief: bool = False) -> Stage_Segment:
    return Stage_Segment(_at(a), _at(b), stage, brief)


# Window k spans [120 k, 120 (k + 1)) seconds.
SEGMENTS = (
    _seg(0, 150, S.light),  # w0: all light; w1: 30 s light
    _seg(150, 210, S.deep),  # w1: 60 s deep, 210-240 uncovered -> unknown
    _seg(240, 300, S.awake),  # w2: 60 awake / 60 rem -> tie -> awake
    _seg(300, 420, S.rem),  # w3: 60 rem / 60 light -> tie -> rem
    _seg(420, 600, S.light),  # w4: light with a Brief_Awakening overlapping it
    _seg(500, 530, S.awake, brief=True),
    _seg(600, 660, S.restless),  # w5: 60 restless / 60 uncovered -> tie -> restless
)


def _session(stages=SEGMENTS) -> SleepSession:
    return SleepSession(start_time=START, end_time=END, source="test", stages=stages)


def _timeline(steps=None) -> Aligned_Timeline:
    ts = sample_timestamps(START, END, RES)
    n = len(ts)
    values, missing = {}, {}
    if steps is not None:
        vals = tuple(steps(i) for i in range(n))
        values[Metric.steps] = vals
        missing[Metric.steps] = tuple(v is None for v in vals)
    return Aligned_Timeline(START, END, RES, ts, values, missing, (S.unknown,) * n)


def _windows(tl):
    return feature_window_bounds(tl, night_compression(tl, 30))


def test_stage_fractions_precedence_unknown_and_sum():
    tl = _timeline()
    windows = _windows(tl)
    cov = window_stage_coverage(_session(), windows)
    assert len(cov) == len(windows)
    for c in cov:
        assert set(c.stage_fractions) == set(DOMINANT_STAGE_ORDER)
        assert abs(math.fsum(c.stage_fractions.values()) - 1.0) <= STAGE_FRACTION_TOLERANCE
        assert all(0.0 <= f <= 1.0 for f in c.stage_fractions.values())
        assert 0.0 <= c.movement_fraction <= 1.0

    assert cov[0].stage_fractions[S.light] == 1.0
    assert cov[1].stage_fractions[S.light] == pytest.approx(0.25)
    assert cov[1].stage_fractions[S.deep] == pytest.approx(0.5)
    assert cov[1].stage_fractions[S.unknown] == pytest.approx(0.25)
    assert cov[1].dominant_stage is S.deep
    # Brief_Awakening takes precedence over the overlapping light segment.
    assert cov[4].stage_fractions[S.awake] == pytest.approx(0.25)
    assert cov[4].stage_fractions[S.light] == pytest.approx(0.75)
    assert cov[4].movement_fraction == pytest.approx(0.25)
    assert cov[5].movement_fraction == pytest.approx(0.5)
    # Beyond the last segment everything is unknown.
    assert cov[6].stage_fractions[S.unknown] == 1.0
    assert cov[6].dominant_stage is S.unknown


def test_dominant_stage_tie_order():
    cov = window_stage_coverage(_session(), _windows(_timeline()))
    assert cov[2].dominant_stage is S.awake  # awake == rem
    assert cov[3].dominant_stage is S.rem  # rem == light
    assert cov[5].dominant_stage is S.restless  # restless == unknown
    assert dominant_stage({S.deep: 0.5, S.light: 0.5}) is S.light
    assert dominant_stage({S.asleep: 0.4, S.unknown: 0.6}) is S.unknown


def test_session_without_stages_is_unknown():
    cov = window_stage_coverage(_session(stages=()), _windows(_timeline()))
    assert all(c.stage_fractions[S.unknown] == 1.0 for c in cov)
    assert all(c.dominant_stage is S.unknown and c.movement_fraction == 0.0 for c in cov)


def test_movement_from_mean_steps():
    assert movement_intensity_from_mean(None) == 0.0
    assert movement_intensity_from_mean(0.0) == 0.0
    assert movement_intensity_from_mean(MOVEMENT_FULL_SCALE / 2) == pytest.approx(0.5)
    assert movement_intensity_from_mean(10 * MOVEMENT_FULL_SCALE) == 1.0

    def steps(i):
        w = i // 12
        return {0: 15.0, 2: 0.0, 3: 60.0}.get(w)  # other windows: missing

    tl = _timeline(steps)
    windows = _windows(tl)
    report = Processing_Report()
    cov = window_stage_coverage(_session(), windows)
    assert window_mean_steps(tl, windows)[:4] == (15.0, None, 0.0, 60.0)
    mi = window_movement_intensity(tl, windows, cov, report)
    assert mi[:4] == (pytest.approx(0.5), 0.0, 0.0, 1.0)
    assert all(v == 0.0 for v in mi[4:])  # no steps sample -> 0.0, not the stage fallback
    assert report.warnings == []


def test_movement_falls_back_to_stages_with_warning():
    tl = _timeline(steps=lambda i: None)  # steps present but all missing
    windows = _windows(tl)
    cov = window_stage_coverage(_session(), windows)
    report = Processing_Report()
    mi = window_movement_intensity(tl, windows, cov, report)
    assert mi == tuple(c.movement_fraction for c in cov)
    assert mi[4] == pytest.approx(0.25) and mi[5] == pytest.approx(0.5)
    assert [w.description for w in report.warnings] == [STAGE_DERIVED_MOVEMENT_WARNING]

    # A timeline without the steps metric at all also falls back.
    report2 = Processing_Report()
    assert window_movement_intensity(_timeline(), windows, cov, report2) == mi
    assert len(report2.warnings) == 1


def test_assemble_with_real_stage_and_movement_parts():
    tl = _timeline(steps=lambda i: 3.0)
    comp = night_compression(tl, 30)
    windows = feature_window_bounds(tl, comp)
    cov = window_stage_coverage(_session(), windows)
    series = assemble_feature_series(
        windows,
        metric_window_features(tl, windows),
        [c.stage_fractions for c in cov],
        [c.dominant_stage for c in cov],
        window_movement_intensity(tl, windows, cov),
        comp.ratio,
    )
    assert series.dominant_stages[:6] == (S.light, S.deep, S.awake, S.rem, S.light, S.restless)
    assert all(w.movement_intensity == pytest.approx(0.1) for w in series.windows)
