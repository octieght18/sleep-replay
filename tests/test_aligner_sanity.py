"""Sanity checks for the Aligner and Input_Fingerprint (task 11.2).

Full unit and property tests live in the 11.3-11.8 test modules.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from backend.domain.session import SleepSession
from backend.domain.stages import Sleep_Stage, Stage_Segment
from backend.domain.telemetry import Metric, TelemetryPoint
from backend.processing.aligner import align
from backend.processing.fingerprint import input_fingerprint

UTC = timezone.utc
START = datetime(2024, 1, 1, 22, 0, tzinfo=UTC)
END = START + timedelta(hours=1)


def _session(points, stages=()):
    return SleepSession(start_time=START, end_time=END, source="fitbit", stages=stages, telemetry=points)


def _hr(offset_s, value, source="fitbit"):
    return TelemetryPoint(START + timedelta(seconds=offset_s), source, Metric.heart_rate, value, "bpm")


def test_basic_alignment_and_report():
    points = [_hr(s, 60.0 + (s % 7)) for s in range(0, 3600, 5)]
    points += [_hr(100, 80.0), _hr(100, 70.0)]  # duplicates of the point at 100 s
    points += [
        TelemetryPoint(START + timedelta(minutes=m), "sensorpush:A", Metric.temperature, 68.0, "°F")
        for m in range(60)
    ]
    points += [
        TelemetryPoint(START + timedelta(minutes=m), "sensorpush:B", Metric.temperature, 20.0, "°C")
        for m in range(0, 60, 2)
    ]
    points.append(TelemetryPoint(START, "x", Metric.pressure, 1.0, "furlongs"))
    stages = (Stage_Segment(START, START + timedelta(minutes=30), Sleep_Stage.light),)

    timeline, report = align(_session(points, stages))

    assert timeline.resolution_s == 5
    assert timeline.sample_count == 720
    assert set(timeline.values) == set(Metric)
    assert report.removed_duplicates == {("fitbit", "heart_rate"): 2}
    assert report.sensor_selection == [
        {"metric": "temperature", "selected": "sensorpush:A", "ignored": ["sensorpush:B"]}
    ]
    assert all(abs(v - 20.0) < 1e-9 for v in timeline.values[Metric.temperature] if v is not None)
    assert set(report.unavailable_metrics) == {"hrv_rmssd", "steps", "humidity", "pressure"}
    assert any("furlongs" in w.description for w in report.warnings)
    assert timeline.stages[0] is Sleep_Stage.light and timeline.stages[-1] is Sleep_Stage.unknown


def test_order_and_timezone_independence():
    rng = random.Random(7)
    points = [_hr(s, rng.uniform(50, 90)) for s in range(0, 3600, 10)]
    points += [_hr(s, rng.uniform(50, 90)) for s in range(0, 3600, 50)]
    shuffled = points[:]
    rng.shuffle(shuffled)
    tokyo = ZoneInfo("Asia/Tokyo")
    shifted = [
        TelemetryPoint(p.timestamp.astimezone(tokyo), p.source, p.metric, p.value, p.unit) for p in shuffled
    ]
    base = _session(points)
    other = SleepSession(
        start_time=START.astimezone(tokyo), end_time=END.astimezone(tokyo), source="fitbit", telemetry=shifted
    )
    assert align(base) == align(other)
    assert input_fingerprint(base) == input_fingerprint(other)
    assert input_fingerprint(base) != input_fingerprint(_session(points[:-1]))


def test_gap_and_timezone_mismatch_warnings():
    points = [_hr(0, 60.0), _hr(1800, 62.0)]
    nearby = [_hr(-3600 * 5, 55.0)]
    timeline, report = align(_session(points), nearby)
    assert len(report.gaps) == 1
    assert any("gap" in w.description for w in report.warnings)
    assert any("likely incorrect" in w.description for w in report.warnings)
    _, quiet = align(_session(points))
    assert not any("likely incorrect" in w.description for w in quiet.warnings)
