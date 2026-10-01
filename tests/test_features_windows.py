"""Sanity tests for Feature_Windows and per-metric statistics (task 13.1, Req 12.1-12.5, 12.10)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from backend.domain.features import feature_window_count
from backend.domain.stages import Sleep_Stage
from backend.domain.telemetry import Metric
from backend.domain.timeline import Aligned_Timeline, sample_timestamps
from backend.processing.compression import night_compression
from backend.processing.features import (
    FEATURE_METRICS,
    assemble_feature_series,
    feature_window_bounds,
    metric_window_features,
)

START = datetime(2024, 3, 1, 22, 0, tzinfo=timezone.utc)
END = START + timedelta(hours=2)
RES = 10  # seconds -> 720 samples; Target 30 s -> 60 windows of 120 s, 12 samples each


def _timeline(hr_missing=lambda i: False) -> Aligned_Timeline:
    ts = sample_timestamps(START, END, RES)
    n = len(ts)
    # heart_rate rises 6 bpm per hour of Night_Time.
    hr = [None if hr_missing(i) else 60.0 + 6.0 * (i * RES / 3600) for i in range(n)]
    return Aligned_Timeline(
        start_time=START,
        end_time=END,
        resolution_s=RES,
        timestamps=ts,
        values={Metric.heart_rate: tuple(hr)},
        missing={Metric.heart_rate: tuple(v is None for v in hr)},
        stages=(Sleep_Stage.unknown,) * n,
    )


def test_windows_cover_session_with_required_count():
    tl = _timeline()
    comp = night_compression(tl, 30)
    windows = feature_window_bounds(tl, comp)
    assert len(windows) == feature_window_count(30) == 60
    assert windows[0].start_time == START
    assert windows[-1].end_time == END
    for prev, cur in zip(windows, windows[1:]):
        assert cur.start_time == prev.end_time
        assert cur.sample_start == prev.sample_stop
    assert windows[-1].sample_stop == tl.sample_count
    assert all(w.sample_count == 12 for w in windows)
    assert all(w.end_time - w.start_time == timedelta(seconds=120) for w in windows)


def test_statistics_slope_and_smoothing():
    tl = _timeline()
    windows = feature_window_bounds(tl, night_compression(tl, 30))
    feats = metric_window_features(tl, windows)
    assert all(set(f) == set(FEATURE_METRICS) for f in feats)

    first = feats[0][Metric.heart_rate]
    assert first.minimum == pytest.approx(60.0)
    assert first.maximum == pytest.approx(60.0 + 6.0 * 110 / 3600)
    assert first.minimum <= first.mean <= first.maximum
    assert first.slope == pytest.approx(6.0)  # Canonical_Unit per hour
    assert first.missing_fraction == 0.0
    # 5-min window centred at 60 s is truncated to [0, 210 s] -> samples 0..210 s.
    assert first.smoothed == pytest.approx(60.0 + 6.0 * (105 / 3600))

    # A metric absent from the timeline is entirely missing.
    temp = feats[0][Metric.temperature]
    assert (temp.mean, temp.slope, temp.smoothed, temp.missing_fraction) == (None, None, None, 1.0)


def test_partially_and_fully_missing_windows():
    # Window 1 (samples 12..23) fully missing; window 2 has samples 24..29 missing.
    tl = _timeline(hr_missing=lambda i: 12 <= i < 30)
    windows = feature_window_bounds(tl, night_compression(tl, 30))
    feats = metric_window_features(tl, windows)
    empty = feats[1][Metric.heart_rate]
    assert (empty.mean, empty.minimum, empty.maximum, empty.slope) == (None,) * 4
    assert empty.missing_fraction == 1.0
    assert empty.smoothed is not None  # neighbours lie within the smoothing interval
    assert feats[2][Metric.heart_rate].missing_fraction == pytest.approx(0.5)


def test_assemble_feature_series():
    tl = _timeline()
    comp = night_compression(tl, 30)
    windows = feature_window_bounds(tl, comp)
    feats = metric_window_features(tl, windows)
    n = len(windows)
    series = assemble_feature_series(
        windows,
        feats,
        [{Sleep_Stage.unknown: 1.0}] * n,
        [Sleep_Stage.unknown] * n,
        [0.0] * n,
        comp.ratio,
    )
    assert len(series) == n
    assert series.compression_ratio == comp.ratio
    with pytest.raises(ValueError):
        assemble_feature_series(windows, feats[:-1], [], [], [], comp.ratio)
