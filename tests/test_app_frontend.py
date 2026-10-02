"""Execute the shipped display logic in embedded V8: no Node or browser needed."""

import json
import re
from pathlib import Path

import pytest
from py_mini_racer import MiniRacer

from backend.domain.mapping import DEFAULT_MAPPING
from tests.test_replay_manifest import manifest
from backend.sonification.manifest import manifest_dict

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def js():
    with MiniRacer() as engine:
        for name in (
            "units",
            "timeline",
            "main_screen",
            "settings_panel",
            "import_view",
        ):
            source = (ROOT / "frontend" / "js" / (name + ".js")).read_text()
            source = re.sub(r"^import .*?;\s*", "", source, flags=re.MULTILINE)
            engine.eval(source.replace("export ", ""))
        yield engine


def call(js, expression):
    return json.loads(js.eval("JSON.stringify(" + expression + ")"))


@pytest.mark.parametrize("value,expected", [(0, 32), (20, 68), (-40, -40), (100, 212)])
def test_temperature_conversion(js, value, expected):
    assert js.eval(f"celsiusToFahrenheit({value})") == expected


def test_units_missing_and_formatting(js):
    availability = {
        k: {"status": "available"} for k in ("temperature", "humidity", "pressure")
    }
    assert call(
        js,
        f"environmentalValues([20,45.6,1013.25],{json.dumps(availability)},'imperial')",
    ) == ["68.0 °F", "46%", "29.92 inHg"]
    assert call(
        js,
        f"environmentalValues([20,45.6,1013.25],{json.dumps(availability)},'metric')",
    ) == ["20.0 °C", "46%", "1013.3 hPa"]
    assert call(js, "environmentalValues([20,50,1000],{},'metric')") == []
    assert call(
        js, f"environmentalValues(null,{json.dumps(availability)},'metric')"
    ) == ["temperature: —", "humidity: —", "pressure: —"]


@pytest.mark.parametrize(
    "time,state", [(0, "light"), (14.99, "light"), (15, "deep"), (30, "deep")]
)
def test_segment_boundaries(js, time, state):
    assert call(js, f"segmentAt([[0,15,'light'],[15,30,'deep']],{time},30)")[2] == state


def test_night_time_and_labels(js):
    document = json.dumps(manifest_dict(manifest()))
    assert (
        js.eval(f"nightTime({document},15).toISOString()") == "2024-03-02T07:00:00.000Z"
    )
    assert (
        js.eval(f"formatNight(nightTime({document},15),'America/New_York')")
        == "2:00 AM"
    )
    session = {
        "start_time": "2024-03-01T22:00:00-05:00",
        "end_time": "2024-03-02T06:00:00-05:00",
    }
    assert (
        js.eval(
            f"sessionLabel({json.dumps(session)},'America/New_York',new Date('2024-03-02T12:00:00Z'))"
        )
        == "Last night"
    )
    assert (
        js.eval(
            f"sessionLabel({json.dumps(session)},'America/New_York',new Date('2024-03-03T12:00:00Z'))"
        )
        == "Night of Mar 1, 2024"
    )


@pytest.mark.parametrize(
    "key,position,result",
    [
        ("ArrowLeft", 2, 0),
        ("ArrowRight", 29, 30),
        ("ArrowLeft", 20, 15),
        ("Home", 20, 0),
        ("End", 5, 30),
        ("other", 5, 5),
    ],
)
def test_seek_math(js, key, position, result):
    assert js.eval(f"seekKey({json.dumps(key)},{position},30)") == result


def test_marker_math_and_clock(js):
    assert js.eval("markerPosition({replay_time_s:15},30)") == 50
    assert js.eval("markerPosition({replay_time_s:40},30)") == 100
    assert js.eval("clock(600)") == "10:00"
    assert js.eval("clock(5.99)") == "00:05"


@pytest.mark.parametrize(
    "kind,value,invalid",
    [
        ("random_seed", "4294967295", False),
        ("random_seed", "-1", True),
        ("random_seed", "1.5", True),
        ("random_seed", "", True),
        ("sensitivity", ".05", False),
        ("sensitivity", ".03", True),
        ("sensitivity", "1", False),
        ("sensitivity", "2", True),
        ("sensitivity", "NaN", True),
        ("sound_style", "nature", False),
        ("sound_style", "music", False),
        ("sound_style", "other", True),
    ],
)
def test_setting_validation(js, kind, value, invalid):
    assert (
        bool(js.eval(f"validateSetting({json.dumps(kind)},{json.dumps(value)})"))
        == invalid
    )


def test_defaults_match_backend(js):
    assert call(js, "DEFAULT_TARGETS") == {
        k: v.target.value for k, v in DEFAULT_MAPPING.metrics.items()
    }
    assert call(js, "DEFAULT_SENSITIVITIES") == {
        k: v.sensitivity for k, v in DEFAULT_MAPPING.metrics.items()
    }


@pytest.mark.parametrize(
    "fitbit,sensor,error",
    [
        ([{"name": "sleep.JSON", "size": 2}], [], None),
        (
            [{"name": "export.zip", "size": 2}],
            [{"name": "sensor.CSV", "size": 2}],
            None,
        ),
        (
            [{"name": "export.zip", "size": 2}, {"name": "sleep.json", "size": 2}],
            [],
            "ZIP",
        ),
        ([], [{"name": "bad.json", "size": 2}], "unsupported"),
        ([{"name": "sleep.json", "size": 11}], [], "limit"),
        ([], [], "No files"),
    ],
)
def test_file_validation(js, fitbit, sensor, error):
    value = call(js, f"validateFiles({json.dumps(fitbit)},{json.dumps(sensor)},10)")
    if error:
        assert error in value["description"]
    else:
        assert value is None
