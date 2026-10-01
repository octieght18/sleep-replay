import json
from dataclasses import replace
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from hypothesis import given, settings, strategies as st

from backend.domain.mapping import DEFAULT_MAPPING, METRIC_KEYS
from backend.domain.replay import Replay_Manifest, Night_Event_Record, Metric_Availability
from backend.sonification.manifest import manifest_dict, parse_manifest, serialize_manifest


def manifest(seed=20240301, duration=30, temperature=20.0, magnitude=0.5, unavailable=()):
    start = datetime(2024, 3, 1, 22, tzinfo=ZoneInfo("America/New_York"))
    end = start + timedelta(hours=8)
    config = replace(DEFAULT_MAPPING, target_duration=duration, random_seed=seed)
    return Replay_Manifest(start, end, "America/New_York", duration, 28800 / duration, 5,
        ((0, duration / 2, "light"), (duration / 2, duration, "deep")),
        (Night_Event_Record("sleep onset", start, 0, magnitude, "22:00 sleep onset"),),
        ((temperature, None, 1013.25),) * (duration * 2),
        {k: Metric_Availability("unavailable" if k in unavailable else "available", 1 if k in unavailable else 0.1) for k in METRIC_KEYS},
        tuple(unavailable), config, seed, "a" * 64, "0.2.0", ("Synthetic fixture warning.",))


# Feature: sleep-replay-sonification, Property 19: Replay_Manifest serializer round trip
@settings(max_examples=100)
@given(st.integers(0, 2**32 - 1), st.floats(-10, 40), st.floats(0, 1),
       st.lists(st.sampled_from(METRIC_KEYS), unique=True), st.sampled_from((30, 120, 180, 300, 600)))
def test_manifest_round_trip(seed, temperature, magnitude, unavailable, duration):
    source = manifest(seed, duration, temperature, magnitude, unavailable)
    encoded = serialize_manifest(source)
    assert parse_manifest(encoded) == source
    assert serialize_manifest(parse_manifest(encoded)) == encoded
    assert encoded.endswith(b"\n") and b"\r" not in encoded
    decoded = json.loads(encoded)
    assert decoded["session_start"] == "2024-03-01T22:00:00-05:00"
    assert set(decoded) == set(manifest_dict(source))
    assert "generation_time" not in decoded and "telemetry" not in decoded and "samples" not in decoded


@pytest.mark.parametrize("field", tuple(manifest_dict(manifest())))
def test_all_fields_required(field):
    document = manifest_dict(manifest())
    del document[field]
    with pytest.raises(ValueError, match=field):
        parse_manifest(json.dumps(document))


@pytest.mark.parametrize("path,value", [
    (("night_events", 0, "magnitude"), -0.1), (("night_events", 0, "magnitude"), 1.1),
    (("night_events", 0, "replay_time_s"), 31), (("night_events", 0, "replay_time_s"), -1),
    (("night_events", 0, "magnitude"), float("nan")), (("night_events", 0, "night_time"), "2024-03-01T22:00:00"),
    (("session_start",), "2024-03-01T22:00:00+03:00"), (("coarse_states",), [[1, 30, "light"]]),
    (("coarse_states",), [[0, 20, "deep"], [19, 30, "light"]]),
    (("coarse_states",), [[0, 10, "deep"], [11, 30, "light"]]),
    (("coarse_states",), [[0, 29, "deep"]]), (("coarse_states",), [[0, 30, "bogus"]]),
    (("availability", "hrv", "missing_fraction"), 2), (("environmental_windows", 0, 0), "warm"),
    (("environmental_windows",), []), (("warnings",), [3]), (("input_fingerprint",), ""),
    (("target_duration_s",), True), (("compression_ratio",), 1),
])
def test_rejections_identify_field(path, value):
    document = manifest_dict(manifest())
    target = document
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(ValueError, match=path[0]):
        parse_manifest(json.dumps(document))


def test_invalid_json_duplicates_and_unexpected_samples():
    for document in ("[", '{"version": "one", "version": "two"}'):
        with pytest.raises(ValueError):
            parse_manifest(document)
    data = manifest_dict(manifest())
    data["telemetry"] = []
    with pytest.raises(ValueError, match="telemetry"):
        parse_manifest(json.dumps(data))


def test_timestamp_fraction_and_dst_offset_round_trip():
    m = manifest()
    start = datetime(2024, 11, 3, 0, 30, 0, 123456, tzinfo=ZoneInfo("America/New_York"))
    end = datetime(2024, 11, 3, 8, 30, 0, 123456, tzinfo=ZoneInfo("America/New_York"))
    m = replace(m, session_start=start, session_end=end, compression_ratio=9 * 3600 / 30,
        night_events=(replace(m.night_events[0], night_time=start),))
    assert parse_manifest(serialize_manifest(m)) == m
