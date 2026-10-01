"""Sanity checks for backend.processing.resample (full tests come in tasks 11.3-11.8)."""

from datetime import datetime, timedelta, timezone

from backend.domain.stages import Sleep_Stage, Stage_Segment
from backend.domain.telemetry import Metric
from backend.processing.resample import (
    Gap,
    assign_stages,
    choose_resolution,
    resample_continuous,
    resample_steps,
    sample_grid,
)

T0 = datetime(2024, 3, 1, 22, 0, tzinfo=timezone.utc)


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def test_choose_resolution_and_fallback():
    hr = [(at(s), 60.0) for s in range(0, 100, 7)]  # median 7 s -> 10 s
    temp = [(at(s), 20.0) for s in range(0, 1000, 120)]  # median 120 s
    assert choose_resolution({Metric.heart_rate: hr, Metric.temperature: temp}) == 10
    assert choose_resolution({Metric.temperature: temp}) == 60
    assert choose_resolution({Metric.heart_rate: [(at(0), 60.0)]}) == 60
    assert choose_resolution({Metric.steps: [(at(0), 1.0), (at(1), 2.0)]}) == 60


def test_grid_count_is_ceiling():
    grid = sample_grid(T0, at(125), 60)
    assert grid == (at(0), at(60), at(120))


def test_mean_interpolation_gap_and_no_extrapolation():
    grid = sample_grid(T0, at(600), 60)  # 10 samples
    series = [(at(70), 10.0), (at(90), 20.0), (at(240), 40.0), (at(599), 1.0)]
    values, missing, gaps = resample_continuous(
        Metric.heart_rate, series, grid, at(600), timedelta(seconds=150)
    )
    assert missing[0] and values[0] is None  # before first value: no extrapolation
    assert values[1] == 15.0  # mean of 10 and 20 in [60, 120)
    # [120, 180) empty; brackets 90 s and 240 s are exactly 150 s apart -> interpolate
    assert abs(values[2] - (20.0 + 20.0 * 30 / 150)) < 1e-9
    assert values[4] == 40.0
    # 240 s -> 599 s is > 150 s: samples 5..8 missing, one gap entry
    assert all(missing[5:9]) and values[9] == 1.0
    assert gaps == (Gap(Metric.heart_rate, at(240), timedelta(seconds=359)),)


def test_value_at_end_time_is_bracket_not_mean():
    grid = sample_grid(T0, at(120), 60)
    series = [(at(10), 5.0), (at(120), 15.0)]
    values, missing, _ = resample_continuous(
        Metric.temperature, series, grid, at(120), timedelta(minutes=15)
    )
    assert values[0] == 5.0
    assert abs(values[1] - (5.0 + 10.0 * 50 / 110)) < 1e-9


def test_steps_hold_and_stages():
    grid = sample_grid(T0, at(180), 30)
    values, missing = resample_steps([(at(0), 12.0), (at(120), 3.0)], grid)
    assert values == (12.0, 12.0, None, None, 3.0, 3.0)
    assert missing == (False, False, True, True, False, False)
    segments = [Stage_Segment(at(30), at(90), Sleep_Stage.deep)]
    assert assign_stages(segments, grid) == (
        Sleep_Stage.unknown,
        Sleep_Stage.deep,
        Sleep_Stage.deep,
        Sleep_Stage.unknown,
        Sleep_Stage.unknown,
        Sleep_Stage.unknown,
    )


def test_default_max_gap_boundary_is_inclusive():
    # heart_rate Max_Interpolation_Gap is 5 min; sample at 60 s has an empty interval.
    grid = sample_grid(T0, at(360), 60)
    exact = [(at(0), 50.0), (at(300), 80.0)]
    values, missing, gaps = resample_continuous(Metric.heart_rate, exact, grid, at(360))
    assert not any(missing[:5]) and gaps == ()
    assert abs(values[1] - 56.0) < 1e-9

    over = [(at(0), 50.0), (T0 + timedelta(seconds=300, microseconds=1), 80.0)]
    values, missing, gaps = resample_continuous(Metric.heart_rate, over, grid, at(360))
    assert values[0] == 50.0 and all(missing[1:5]) and values[5] == 80.0
    assert gaps == (Gap(Metric.heart_rate, at(0), timedelta(seconds=300, microseconds=1)),)


def test_input_order_and_offsets_do_not_matter():
    plus2 = timezone(timedelta(hours=2))
    series = [(at(s), float(s % 7)) for s in range(0, 900, 45)]
    shuffled = [(ts.astimezone(plus2), v) for ts, v in reversed(series)]
    grid = sample_grid(T0, at(900), 30)
    assert resample_continuous(Metric.humidity, series, grid, at(900)) == resample_continuous(
        Metric.humidity, shuffled, tuple(t.astimezone(plus2) for t in grid), at(900)
    )


def test_grid_is_uniform_in_utc_elapsed_time_across_dst():
    from zoneinfo import ZoneInfo

    ny = ZoneInfo("America/New_York")  # 2024-11-03 fall-back: 22:00 -> 06:00 is 9 h
    grid = sample_grid(datetime(2024, 11, 2, 22, tzinfo=ny), datetime(2024, 11, 3, 6, tzinfo=ny), 60)
    assert len(grid) == 9 * 60
    assert {b - a for a, b in zip(grid, grid[1:])} == {timedelta(seconds=60)}


def test_naive_timestamps_are_rejected():
    import pytest

    grid = sample_grid(T0, at(120), 60)
    with pytest.raises(ValueError):
        resample_continuous(Metric.heart_rate, [(datetime(2024, 3, 1, 22), 60.0)], grid, at(120))
