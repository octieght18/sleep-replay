"""Generator determinism, round trip, and committed sample targets (requirement 14)."""
import json
from datetime import timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from hypothesis import given, settings, strategies as st, HealthCheck

from backend.domain.adapter import TimezoneContext
from backend.domain.sample_dataset import SAMPLE_RANDOM_SEED, SAMPLE_TIMEZONE
from backend.domain.stages import Sleep_Stage
from backend.domain.telemetry import Metric
from backend.domain.timezones import FILE_TYPES
from backend.ingestion.fitbit.importer import FitbitImporter
from backend.ingestion.sensorpush.importer import SensorPushImporter
from tools.sample_data_generator import SyntheticNight, generate_night, write_night, dataset_bytes, validate_night

SAMPLE_DIR = Path(__file__).resolve().parents[1] / "sample_data"


def imported(directory):
    context = TimezoneContext(ZoneInfo("UTC"), {ft: SAMPLE_TIMEZONE for ft in FILE_TYPES})
    fitbit = FitbitImporter().load(sorted(p for p in directory.iterdir() if p.name != "sensorpush.csv"), context)
    sensor = SensorPushImporter().load([directory / "sensorpush.csv"], context)
    return fitbit, sensor


# Feature: sleep-replay-data-pipeline, Property 21: Sample_Data_Generator determinism
@settings(max_examples=5, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(seed=st.integers())
def test_property_21_generator_bytes(seed, tmp_data_dir):
    """Validates requirement 14.8 by regenerating and writing both independent nights."""
    a, b = generate_night(seed), generate_night(seed)
    assert dataset_bytes(a) == dataset_bytes(b)
    first, second = tmp_data_dir / "first", tmp_data_dir / "second"
    write_night(a, first)
    write_night(b, second)
    assert {p.name: p.read_bytes() for p in first.iterdir()} == {p.name: p.read_bytes() for p in second.iterdir()}


def test_documented_seed_matches_committed_sample():
    night = generate_night(SAMPLE_RANDOM_SEED)
    assert dataset_bytes(night) == {p.name: p.read_bytes() for p in SAMPLE_DIR.iterdir()}
    assert all(b"\r" not in content for content in dataset_bytes(night).values())


# Feature: sleep-replay-data-pipeline, Property 22: Sample data generate-write-import round trip
@settings(max_examples=5, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(seed=st.integers())
def test_property_22_generate_import(seed, tmp_data_dir):
    """Validates requirement 14.9 using both real file importers, not serializer helpers."""
    night = generate_night(seed)
    directory = tmp_data_dir / "roundtrip"
    write_night(night, directory)
    fitbit, sensor = imported(directory)
    assert len(fitbit.sessions) == 1
    assert fitbit.sessions[0].stages == night.session.stages
    key = lambda p: (p.timestamp.astimezone(timezone.utc), p.source, p.metric.value)
    actual = sorted([*fitbit.telemetry, *sensor.telemetry], key=key)
    expected = sorted(night.telemetry, key=key)
    assert len(actual) == len(expected)
    for a, b in zip(actual, expected):
        assert key(a) == key(b) and a.unit == b.unit
        assert abs(a.value - b.value) <= .0005
    for report in (fitbit.report, sensor.report):
        assert not report.skipped_files and not report.warnings
        assert report.skipped_row_count == report.skipped_value_count == 0


def test_committed_dataset_targets():
    fitbit, sensor = imported(SAMPLE_DIR)
    generated = generate_night(SAMPLE_RANDOM_SEED)
    session = fitbit.sessions[0]
    night = SyntheticNight(session, tuple([*fitbit.telemetry, *sensor.telemetry]),
                           generated.levels, generated.brief_awakenings)
    validate_night(night)
    raw = json.loads((SAMPLE_DIR / "sleep-2024-03-01.json").read_bytes())
    assert len(raw) == 1 and raw[0]["type"] == "stages" and raw[0]["isMainSleep"] is True
    assert session.end_time - session.start_time == timedelta(hours=8)
    assert len([s for s in session.stages if s.is_brief_awakening]) == 3
    rem = [s for s in session.stages if s.stage is Sleep_Stage.rem]
    assert len(rem) == 5 and all(s.end_time - s.start_time >= timedelta(minutes=5) for s in rem)
    assert len([p for p in fitbit.telemetry if p.metric is Metric.hrv_rmssd]) == 96
    assert len([p for p in fitbit.telemetry if p.metric is Metric.steps]) == 480
    assert len(sensor.telemetry) == 450 * 3
