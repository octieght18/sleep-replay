import json
from dataclasses import replace

import pytest
import yaml
from hypothesis import given, settings, strategies as st

from backend.domain.errors import ERROR_CODES, User_Error
from backend.domain.mapping import DEFAULT_MAPPING, METRIC_KEYS, HYSTERESIS_MAX, Mapping_Config, Metric_Mapping, mapping_dict
from backend.domain.sound import Breakpoint, Mapping_Target, Parameter_Trajectory
from backend.sonification.config_parser import MAX_CONFIG_BYTES, parse_config
from backend.sonification.config_printer import print_config


@st.composite
def configs(draw):
    metrics = {}
    for key in METRIC_KEYS:
        metrics[key] = Metric_Mapping(draw(st.sampled_from(tuple(Mapping_Target))), draw(st.floats(0, 1)),
            None if key == "movement" else draw(st.integers(1, 60)),
            None if key == "movement" else draw(st.floats(0, HYSTERESIS_MAX[key])))
    return Mapping_Config(metrics, draw(st.sampled_from((30, 120, 180, 300, 600))), draw(st.integers(0, 2**32 - 1)), draw(st.sampled_from(("music", "nature"))))


# Feature: sleep-replay-sonification, Property 1: Mapping_Config parse-print-parse round trip
@settings(max_examples=100)
@given(configs())
def test_config_round_trip(config):
    printed = print_config(config)
    assert parse_config(printed) == config
    assert print_config(parse_config(printed)) == printed
    assert "\r" not in printed


# Feature: sleep-replay-sonification, Property 2: Mapping_Config format equivalence
@settings(max_examples=100)
@given(configs())
def test_format_equivalence(config):
    document = mapping_dict(config)
    assert parse_config(json.dumps(document)) == parse_config(yaml.safe_dump(document)) == config


@pytest.mark.parametrize("document", ["", "---\n", "\n# empty\n", "{}", "heart_rate: {}", "heart_rate:\n  sensitivity: 0.5"])
def test_per_value_defaults(document):
    assert parse_config(document) == DEFAULT_MAPPING
    assert tuple(DEFAULT_MAPPING.metrics) == METRIC_KEYS
    with pytest.raises(TypeError):
        DEFAULT_MAPPING.metrics["heart_rate"] = None


@pytest.mark.parametrize("key", METRIC_KEYS)
@pytest.mark.parametrize("target", tuple(Mapping_Target))
def test_all_targets(key, target):
    assert parse_config(json.dumps({key: {"target": target}})).metrics[key].target == target


@pytest.mark.parametrize("key", METRIC_KEYS)
@pytest.mark.parametrize("value", [True, False, "0.5", None, -0.01, 1.01, float("nan"), float("inf")])
def test_bad_sensitivities(key, value):
    with pytest.raises(User_Error, match=key + r"\.sensitivity") as error:
        parse_config(json.dumps({key: {"sensitivity": value}}))
    assert error.value.code == "INVALID_MAPPING_CONFIG"
    assert "[0, 1]" in error.value.action


@pytest.mark.parametrize("key", HYSTERESIS_MAX)
@pytest.mark.parametrize("value", [True, "5", None, 0, 61, 3.5])
def test_smoothing_strict_types_and_range(key, value):
    with pytest.raises(User_Error, match=key + r"\.smoothing_minutes"):
        parse_config(json.dumps({key: {"smoothing_minutes": value}}))


@pytest.mark.parametrize("key", HYSTERESIS_MAX)
def test_hysteresis_boundaries(key):
    maximum = HYSTERESIS_MAX[key]
    for value in (0, maximum):
        assert parse_config(json.dumps({key: {"hysteresis": value}})).metrics[key].hysteresis == value
    for value in (-1, maximum + 0.01, True, "1", None, float("inf")):
        with pytest.raises(User_Error, match=key + r"\.hysteresis"):
            parse_config(json.dumps({key: {"hysteresis": value}}))


@pytest.mark.parametrize("document,path", [
    ('{"heart_rate":{"target":"bogus"}}', "heart_rate.target"),
    ('{"Heart_rate":{}}', "Heart_rate"), ('{"metrics":{}}', "metrics"),
    ('movement:\n  smoothing_minutes: 5', "movement.smoothing_minutes"),
    ('movement:\n  hysteresis: 0.5', "movement.hysteresis"),
    ('heart_rate:\n  sensitivity: 0.1\n  sensitivity: 0.3', "heart_rate.sensitivity"),
    ('{"heart_rate":{"target":"none","target":"pulse_rate"}}', "heart_rate.target"),
    ('heart_rate: []', "heart_rate"), ('null', "document"), ('[]', "document"),
    ('heart_rate:\n  target: None', "heart_rate.target"), ('temperature:\n  target: Brightness', "temperature.target"),
    ('x: &x {x: *x}', "x.x"),
])
def test_bad_keys_and_duplicates(document, path):
    previous = replace(DEFAULT_MAPPING, random_seed=9)
    with pytest.raises(User_Error) as error:
        parse_config(document)
    assert path in error.value.description
    assert previous.random_seed == 9


@pytest.mark.parametrize("field,value", [("target_duration", 60), ("target_duration", 30.0),
    ("target_duration", True), ("target_duration", "30"), ("random_seed", -1),
    ("random_seed", 2**32), ("random_seed", 3.5), ("random_seed", True), ("random_seed", "3")])
def test_global_validation(field, value):
    with pytest.raises(User_Error, match=field):
        parse_config(json.dumps({field: value}))


def test_size_encoding_syntax_and_unsafe_tags():
    assert parse_config(b"{}" + b" " * (MAX_CONFIG_BYTES - 2)) == DEFAULT_MAPPING
    for data, fragment in [(b" " * (MAX_CONFIG_BYTES + 1), "64 KB"), (b"\xff", "UTF-8"),
                           ("heart_rate:\n  target: [", "line"), ("!!python/object/apply:os.system [echo bad]", "line")]:
        with pytest.raises(User_Error) as error:
            parse_config(data)
        assert fragment in str(error.value)


def test_trajectory_evaluation_and_new_errors():
    trajectory = Parameter_Trajectory((Breakpoint(1, 0.2), Breakpoint(3, 0.8)))
    assert trajectory.value_at(-10) == 0.2
    assert trajectory.value_at(2) == pytest.approx(0.5)
    assert trajectory.value_at(20) == 0.8
    assert {"INVALID_MAPPING_CONFIG", "INVALID_RANDOM_SEED", "NO_USABLE_DATA", "RENDERING_FAILED", "REPLAY_WRITE_FAILED", "GENERATION_IN_PROGRESS"} <= ERROR_CODES
