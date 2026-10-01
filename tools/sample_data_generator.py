"""Deterministic synthetic night; no personal data or external service is used.

Run: python -m tools.sample_data_generator --seed 20240301 --out sample_data
All sample timestamps are written in SAMPLE_TIMEZONE, including Fitbit intraday
timestamps (override their usual UTC default when importing this synthetic data).
"""
from __future__ import annotations

import argparse
import json
import math
import random
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from backend.domain.events import Event_Type
from backend.domain.sample_dataset import SAMPLE_RANDOM_SEED, SAMPLE_TIMEZONE
from backend.domain.session import SleepSession
from backend.domain.stages import Sleep_Stage, Stage_Interval, build_stage_segments
from backend.domain.telemetry import CANONICAL_UNIT, Metric, TelemetryPoint
from backend.processing.aligner import align
from backend.processing.events import detect_environmental_events, detect_movement_events
from backend.processing.features import extract
from backend.processing.session_detector import attach_telemetry


@dataclass(frozen=True)
class SyntheticNight:
    session: SleepSession
    telemetry: tuple[TelemetryPoint, ...]
    levels: tuple[Stage_Interval, ...]
    brief_awakenings: tuple[Stage_Interval, ...]


def generate_night(seed: int = SAMPLE_RANDOM_SEED) -> SyntheticNight:
    rng = random.Random(seed)
    start = datetime(2024, 3, 1, 22, tzinfo=ZoneInfo(SAMPLE_TIMEZONE))
    end = start + timedelta(hours=8)
    schedule = [
        ("awake", 10), ("light", 20), ("deep", 45), ("light", 15), ("rem", 10),
        ("light", 20), ("deep", 35), ("awake", 10), ("light", 20), ("rem", 15),
        ("light", 20), ("deep", 25), ("light", 30), ("rem", 20), ("awake", 10),
        ("light", 25), ("deep", 15), ("light", 25), ("rem", 25), ("light", 35),
        ("rem", 30), ("light", 10), ("awake", 10),
    ]
    levels = []
    cursor = start
    for stage, minutes in schedule:
        stop = cursor + timedelta(minutes=minutes)
        levels.append(Stage_Interval(cursor, stop, Sleep_Stage(stage)))
        cursor = stop
    brief = tuple(Stage_Interval(
        start + timedelta(minutes=minute), start + timedelta(minutes=minute, seconds=seconds),
        Sleep_Stage.awake, True,
    ) for minute, seconds in ((48, 60), (210, 45), (405, 90)))
    session = SleepSession(start, end, "fitbit", tuple(build_stage_segments(
        [*levels, *brief], start, end)), log_id="synthetic-night", is_main_sleep=True,
        source_file="sleep-2024-03-01.json")
    telemetry = []

    def point(seconds, source, metric, value):
        telemetry.append(TelemetryPoint(start + timedelta(seconds=seconds), source, metric,
                                        value, CANONICAL_UNIT[metric]))

    for seconds in range(0, 28800, 5):
        if 115 * 60 <= seconds < 130 * 60:
            continue
        stage = session.stage_at(start + timedelta(seconds=seconds))
        base = {Sleep_Stage.deep: 50, Sleep_Stage.awake: 72, Sleep_Stage.rem: 62}.get(stage, 57)
        point(seconds, "fitbit", Metric.heart_rate, base + rng.randint(-3, 3))
    for seconds in range(0, 28800, 300):
        point(seconds, "fitbit", Metric.hrv_rmssd, round(rng.uniform(20, 90), 3))
    for minute in range(480):
        steps = 0
        if any(lo <= minute < lo + 4 for lo in (60, 200, 410)):
            steps = rng.randint(26, 30)
        elif 320 <= minute < 338:
            steps = rng.randint(12, 15)
        point(minute * 60, "fitbit", Metric.steps, steps)
        if 300 <= minute < 330:
            continue
        temperature = 22.0 + 2.2 * min(1.0, max(0.0, (minute - 120) / 15))
        humidity = 45.0 + 5.0 * math.sin(minute * math.pi / 50)
        pressure = 1010.0 + 1.8 * min(1.0, minute / 150)
        for metric, value in ((Metric.temperature, temperature), (Metric.humidity, humidity),
                              (Metric.pressure, pressure)):
            point(minute * 60, "sensorpush:sample", metric, round(value + rng.uniform(-0.01, 0.01), 3))
    night = SyntheticNight(session, tuple(telemetry), tuple(levels), brief)
    validate_night(night)
    return night


def validate_night(night: SyntheticNight) -> None:
    """Self-check every structural/signal target, including extracted movement."""
    def require(condition, target):
        if not condition:
            raise ValueError(f"Synthetic night misses target: {target}")

    session = night.session
    start, end = session.start_time, session.end_time
    require(end - start == timedelta(hours=8) and start.utcoffset() == end.utcoffset(), "8h, no DST")
    require(session.is_main_sleep and session.stage_data_availability == "stages", "main stages sleep")
    rem = [s for s in session.stages if s.stage is Sleep_Stage.rem and s.end_time - s.start_time >= timedelta(minutes=5)]
    require(4 <= len(rem) <= 6 and all(b.start_time - a.start_time >= timedelta(hours=1)
                                    for a, b in zip(rem, rem[1:])), "4-6 separated cycles")
    mid = start + timedelta(hours=4)
    def duration(stage, lo, hi):
        return sum((max(timedelta(0), min(s.end_time, hi) - max(s.start_time, lo))
                    for s in session.stages if s.stage is stage), timedelta())
    require(duration(Sleep_Stage.deep, start, mid) > duration(Sleep_Stage.deep, mid, end), "deep early")
    require(duration(Sleep_Stage.rem, mid, end) > duration(Sleep_Stage.rem, start, mid), "REM late")
    awake = [s for s in session.stages if s.stage is Sleep_Stage.awake and not s.is_brief_awakening
             and session.sleep_onset_time < s.start_time < session.final_wake_time
             and timedelta(minutes=5) <= s.end_time - s.start_time <= timedelta(minutes=20)]
    require(len(awake) >= 2 and all(b.start_time - a.start_time > timedelta(minutes=15)
                                  for a, b in zip(awake, awake[1:])), "two awake runs")
    require(len(night.brief_awakenings) >= 3 and all(timedelta(seconds=30) <= s.end_time - s.start_time
                                                  <= timedelta(seconds=90) for s in night.brief_awakenings), "brief awakenings")
    by_metric = {m: sorted((p for p in night.telemetry if p.metric is m), key=lambda p: p.timestamp) for m in Metric}
    hr = by_metric[Metric.heart_rate]
    require(len(hr) / (28800 / 5) >= .9 and all(float(p.value).is_integer() and 45 <= p.value <= 80 for p in hr), "HR range/coverage")
    hr_gaps = [(b.timestamp - a.timestamp).total_seconds() for a, b in zip(hr, hr[1:])]
    require(any(600 <= g <= 1800 for g in hr_gaps) and all(g == 5 or 600 <= g <= 1800 for g in hr_gaps), "5s HR and gap")
    means = {}
    for stage in (Sleep_Stage.deep, Sleep_Stage.awake):
        vals = [p.value for p in hr if session.stage_at(p.timestamp) is stage]
        means[stage] = sum(vals) / len(vals)
    require(means[Sleep_Stage.awake] - means[Sleep_Stage.deep] >= 5, "HR stage contrast")
    hrv = by_metric[Metric.hrv_rmssd]
    require(len(hrv) == 96 and all(20 <= p.value <= 90 for p in hrv)
            and all(b.timestamp - a.timestamp == timedelta(minutes=5) for a, b in zip(hrv, hrv[1:])), "5min HRV")
    steps = by_metric[Metric.steps]
    require(len(steps) == 480 and all(p.timestamp == start + timedelta(minutes=i) for i, p in enumerate(steps)), "every steps minute")
    timeline, _ = align(attach_telemetry(session, night.telemetry))
    features, _, _ = extract(timeline, session, 180)
    movement = detect_movement_events(features)
    bursts = [e for e in movement if e.type is Event_Type.movement]
    require(len(bursts) >= 3 and all(b.night_time - a.night_time > timedelta(minutes=15)
                                   for a, b in zip(bursts, bursts[1:])), "three separated bursts")
    require(any(e.type is Event_Type.restless_period for e in movement), "restless period")
    env = detect_environmental_events(timeline)
    require(any(e.type is Event_Type.temperature_rising for e in env), "smoothed temperature change")
    require(any(e.type is Event_Type.pressure_rising for e in env), "smoothed pressure change")
    for metric in (Metric.temperature, Metric.humidity, Metric.pressure):
        pts = by_metric[metric]
        require(all(35 <= p.value <= 60 for p in pts) if metric is Metric.humidity else True, "humidity range")
        gaps = [(a.timestamp, b.timestamp) for a, b in zip(pts, pts[1:]) if b.timestamp - a.timestamp > timedelta(minutes=1)]
        require(any(timedelta(minutes=20) <= b - a <= timedelta(minutes=60) for a, b in gaps), "environment gap")
        require(all(b.timestamp - a.timestamp == timedelta(minutes=1) or
                    timedelta(minutes=20) <= b.timestamp - a.timestamp <= timedelta(minutes=60)
                    for a, b in zip(pts, pts[1:])), "1min environment cadence")
        # The modeled change spans end by minute 150; the missing interval is later.
        require(all(a >= start + timedelta(minutes=150) for a, _ in gaps), "gap avoids changes")


def dataset_bytes(night: SyntheticNight) -> dict[str, bytes]:
    """Fixed key order, decimal precision, UTF-8 and LF on every operating system."""
    zone = ZoneInfo(SAMPLE_TIMEZONE)
    def local(t):
        return t.astimezone(zone).replace(tzinfo=None).isoformat(timespec="seconds")
    def level(s):
        return {"dateTime": local(s.start_time), "level": "wake" if s.stage is Sleep_Stage.awake else s.stage.value,
                "seconds": int((s.end_time - s.start_time).total_seconds())}
    session = night.session
    sleep = [{"logId": session.log_id, "type": "stages", "isMainSleep": True,
              "startTime": local(session.start_time), "endTime": local(session.end_time),
              "levels": {"data": [level(s) for s in night.levels], "shortData": [level(s) for s in night.brief_awakenings]}}]
    by_metric = {m: [p for p in night.telemetry if p.metric is m] for m in Metric}
    def json_bytes(value):
        return (json.dumps(value, ensure_ascii=True, allow_nan=False, indent=2) + "\n").encode("utf-8")
    result = {"sleep-2024-03-01.json": json_bytes(sleep)}
    for metric in (Metric.heart_rate, Metric.steps):
        result[f"{metric.value}-2024-03-01.json"] = json_bytes([
            {"dateTime": p.timestamp.astimezone(zone).strftime("%m/%d/%y %H:%M:%S"),
             "value": {"bpm": p.value} if metric is Metric.heart_rate else str(p.value)}
            for p in by_metric[metric]])
    hrv = "timestamp,rmssd\n" + "".join(f"{local(p.timestamp)},{p.value:.3f}\n" for p in by_metric[Metric.hrv_rmssd])
    result["Heart Rate Variability Details - 2024-03-01.csv"] = hrv.encode("utf-8")
    env = "Timestamp,Temperature (°C),Relative Humidity (%),Pressure (hPa),Sensor ID\n"
    env += "".join(f"{local(t.timestamp)},{t.value:.3f},{h.value:.3f},{p.value:.3f},sample\n" for t, h, p in zip(
        by_metric[Metric.temperature], by_metric[Metric.humidity], by_metric[Metric.pressure]))
    result["sensorpush.csv"] = env.encode("utf-8")
    return result


def write_night(night: SyntheticNight, out: str | Path) -> tuple[Path, ...]:
    validate_night(night)
    directory = Path(out)
    directory.mkdir(parents=True, exist_ok=True)
    paths = []
    for name, content in dataset_bytes(night).items():
        path = directory / name
        path.write_bytes(content)
        paths.append(path)
    return tuple(paths)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=SAMPLE_RANDOM_SEED)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    write_night(generate_night(args.seed), args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
