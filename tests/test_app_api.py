"""Application contracts over the real pipeline, with a cheap renderer only."""

import io
import socket
import tempfile
import threading
import time
import wave
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest
from fastapi.testclient import TestClient
from hypothesis import given, settings, strategies as st

from backend.api.app import create_app
from backend.api.errors import INTERNAL_CODES
from backend.api.replay_cache import ReplayCache, cache_key
from backend.api.settings_service import DEFAULT_SETTINGS, SettingsService
from backend.domain.errors import ERROR_CODES, User_Error
from backend.domain.mapping import METRIC_KEYS, TARGET_DURATIONS
from backend.domain.sound import Mapping_Target
from backend.persistence.metadata_store import MetadataStore
from tests.test_replay_manifest import manifest as fixture_manifest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def client(tmp_path):
    with TestClient(
        create_app(tmp_path, display_timezone="America/New_York")
    ) as client:
        yield client


@pytest.fixture
def renderer():
    # Audio fidelity has its own suite. Keep genuine processing, manifests and
    # storage in API tests; a deterministic tiny waveform replaces synthesis.
    with patch(
        "backend.sonification.generation.render",
        return_value=np.concatenate(
            [np.zeros((1, 2)), np.tile([0.1, -0.1], (128, 1)), np.zeros((1, 2))]
        ),
    ) as renderer:
        yield renderer


def sample_settings(client, duration=30):
    assert client.post("/api/imports/sample").status_code == 200
    data = client.get("/api/settings").json()
    data["target_duration"] = duration
    assert client.put("/api/settings", json=data).status_code == 200
    return data


def check_error(response, code=None):
    assert 400 <= response.status_code < 600
    value = response.json()
    assert {"code", "description", "action", "file_name"} <= value.keys()
    assert value["description"] and value["action"]
    if code:
        assert value["code"] == code
    return value


def test_sample_sessions_and_manual_selection(client):
    check_error(client.get("/api/sessions"), "NO_SLEEP_SESSION")
    response = client.post("/api/imports/sample")
    assert response.status_code == 200
    result = response.json()
    assert result["report"]["accepted_files"]
    assert result["report"]["per_metric_counts"]
    assert result["selected_id"] == result["sessions"][0]["key"]
    assert result["sessions"][0]["stage_data_availability"] == "stages"
    assert result["display_timezone"] == "America/New_York"
    assert client.get("/api/sessions").json() == {
        k: v for k, v in result.items() if k != "report"
    }
    old = result["selected_id"]
    for start, end in [
        ("bad", "bad"),
        (None, 8),
        ("2024-03-01T22:00", "2024-03-01T21:00"),
        ("2024-03-01T22:00", "2024-03-03T22:01"),
    ]:
        check_error(
            client.post("/api/sessions/manual", json=dict(start=start, end=end)),
            "INVALID_MANUAL_RANGE",
        )
        assert client.get("/api/sessions").json()["selected_id"] == old
    manual = client.post(
        "/api/sessions/manual",
        json=dict(start="2024-03-01T22:00", end="2024-03-02T06:00"),
    )
    assert manual.status_code == 200
    assert manual.json()["selected"]["stage_data_availability"] == "none"
    assert (
        client.put("/api/sessions/selected", json={"id": old}).json()["selected_id"]
        == old
    )
    check_error(client.put("/api/sessions/selected", json={"id": "missing"}))
    assert client.get("/api/sessions").json()["selected_id"] == old


@pytest.mark.parametrize(
    "files,data,code",
    [
        ([("fitbit", ("bad.exe", b"text"))], {}, "SOURCE_LOAD_FAILED"),
        ([("sensorpush", ("bad.json", b"{}"))], {}, "SOURCE_LOAD_FAILED"),
        (
            [
                ("fitbit", ("one.zip", b"text")),
                ("fitbit", ("sleep-2024-03-01.json", b"[]")),
            ],
            {},
            "SOURCE_LOAD_FAILED",
        ),
        (
            [("fitbit", ("sleep-2024-03-01.json", b"[]"))],
            {"timezone_overrides": '{"fitbit_sleep":"No/Such_Zone"}'},
            "INVALID_TIMEZONE",
        ),
        (
            [("fitbit", ("sleep-2024-03-01.json", b"[]"))],
            {"timezone_overrides": "[]"},
            "SOURCE_LOAD_FAILED",
        ),
        ([("fitbit", ("../escape.json", b"[]"))], {}, "SOURCE_LOAD_FAILED"),
        ([("fitbit", ("sleep:export.json", b"[]"))], {}, "SOURCE_LOAD_FAILED"),
        ([("fitbit", ("CON.json", b"[]"))], {}, "SOURCE_LOAD_FAILED"),
    ],
)
def test_import_rejections_clean_up(client, files, data, code):
    check_error(client.post("/api/imports", files=files, data=data), code)
    assert list((client.app.state.pipeline.data_dir / "uploads").iterdir()) == []
    assert not client.app.state.pipeline.metadata_store.list_import_ids()


def test_real_multipart_import_and_limit(client):
    sleep = next((ROOT / "sample_data").glob("sleep*.json"))
    result = client.post(
        "/api/imports",
        files=[("fitbit", (sleep.name, sleep.read_bytes()))],
        data={"timezone_overrides": '{"fitbit_sleep":"America/New_York"}'},
    )
    assert result.status_code == 200
    assert sleep.name in result.json()["report"]["accepted_files"]
    selected = result.json()["selected_id"]
    client.app.state.max_upload_bytes = 8
    error = check_error(
        client.post("/api/imports", files={"fitbit": ("sleep.json", b"a" * 9)})
    )
    assert error["file_name"] == "sleep.json" and "limit" in error["description"]
    assert client.get("/api/sessions").json()["selected_id"] == selected
    assert list((client.app.state.pipeline.data_dir / "uploads").iterdir()) == []


@pytest.mark.parametrize(
    "key,value,code",
    [
        ("target_duration", 31, "INVALID_TARGET_DURATION"),
        ("random_seed", -1, "INVALID_RANDOM_SEED"),
        ("random_seed", True, "INVALID_RANDOM_SEED"),
        ("display_units", "other", "INVALID_MAPPING_CONFIG"),
        (
            "sensitivities",
            {**DEFAULT_SETTINGS.to_dict()["sensitivities"], "temperature": 0.03},
            "INVALID_MAPPING_CONFIG",
        ),
        (
            "targets",
            {**DEFAULT_SETTINGS.to_dict()["targets"], "temperature": "bad"},
            "INVALID_MAPPING_CONFIG",
        ),
    ],
)
def test_settings_rejection_preserves_record(client, key, value, code):
    original = client.get("/api/settings").json()
    assert original == DEFAULT_SETTINGS.to_dict()
    check_error(client.put("/api/settings", json={**original, key: value}), code)
    assert client.get("/api/settings").json() == original


def test_settings_time_and_no_generation(client):
    data = DEFAULT_SETTINGS.to_dict()
    data["display_units"] = "metric"
    with patch.object(client.app.state.pipeline, "generate") as generate:
        started = time.monotonic()
        assert client.put("/api/settings", json=data).json() == data
        assert time.monotonic() - started < 1
        generate.assert_not_called()


setting_values = st.fixed_dictionaries(
    {
        "target_duration": st.sampled_from(TARGET_DURATIONS),
        "random_seed": st.integers(0, 2**32 - 1),
        "display_units": st.sampled_from(("imperial", "metric")),
        "targets": st.fixed_dictionaries(
            {k: st.sampled_from([t.value for t in Mapping_Target]) for k in METRIC_KEYS}
        ),
        "sensitivities": st.fixed_dictionaries(
            {k: st.integers(0, 20).map(lambda v: v / 20) for k in METRIC_KEYS}
        ),
    }
)


# Feature: sleep-replay-app, Property 1: Settings persistence round trip
@settings(max_examples=100)
@given(setting_values)
def test_settings_restart_round_trip(value):
    with tempfile.TemporaryDirectory() as directory:
        with MetadataStore(directory) as store:
            assert SettingsService(store).put(value).to_dict() == value
        with MetadataStore(directory) as reopened:
            assert SettingsService(reopened).get().to_dict() == value


# Feature: sleep-replay-app, Property 3: Error response structure and status class
@settings(max_examples=100)
@given(
    st.sampled_from(sorted(ERROR_CODES)),
    st.sampled_from(("status", "settings", "sessions", "replays")),
    st.booleans(),
)
def test_api_error_property(code, route, unexpected):
    with tempfile.TemporaryDirectory() as directory:
        app = create_app(directory, display_timezone="UTC")

        @app.get("/injected/" + route)
        def fail():
            if unexpected:
                raise RuntimeError("sensitive telemetry must never be serialized")
            raise User_Error(
                code, "Injected condition.", "Retry with valid data.", "export.csv"
            )

        with TestClient(app) as client:
            response = client.get("/injected/" + route)
            value = check_error(response)
            assert response.status_code // 100 == (
                5 if unexpected or code in INTERNAL_CODES else 4
            )
            if unexpected:
                assert value["reference_id"] in value["action"]
                assert "sensitive telemetry" not in response.text
            else:
                assert value["code"] == code and value["file_name"] == "export.csv"


def test_origin_guard_blocks_before_action(client):
    with patch.object(client.app.state.pipeline, "use_sample_data") as sample:
        for origin in ("http://example.com", "null", "http://127.0.0.1:9999"):
            response = client.post("/api/imports/sample", headers={"Origin": origin})
            assert response.status_code == 403
            check_error(response)
        sample.assert_not_called()
    assert (
        client.get(
            "/api/settings", headers={"Origin": "http://localhost:8734"}
        ).status_code
        == 200
    )
    assert (
        client.get("/api/status", headers={"Origin": "https://evil.test"}).status_code
        == 403
    )


def test_api_flow_cache_range_and_changed_settings(client, renderer):
    data = sample_settings(client)
    first = client.post("/api/replays").json()
    assert first["cached"] is False
    manifest = client.get(first["manifest_url"]).json()
    assert manifest["target_duration_s"] == 30
    audio = client.get(first["audio_url"])
    assert audio.status_code == 200 and audio.headers["content-type"] == "audio/wav"
    with wave.open(io.BytesIO(audio.content)) as reader:
        assert reader.getframerate() == 44100
    assert (
        client.get(first["audio_url"], headers={"Range": "bytes=0-43"}).content
        == audio.content[:44]
    )
    partial = client.get(first["audio_url"], headers={"Range": "bytes=0-43"})
    assert partial.status_code == 206 and partial.headers["content-range"].startswith(
        "bytes 0-43/"
    )
    check_error(client.get(first["audio_url"], headers={"Range": "bytes=9999999-"}))
    assert client.head(first["audio_url"]).status_code == 200
    started = time.monotonic()
    hit = client.post("/api/replays").json()
    assert time.monotonic() - started < 2
    assert hit["cached"] is True and hit["id"] == first["id"]
    assert renderer.call_count == 1
    data["random_seed"] += 1
    assert client.put("/api/settings", json=data).status_code == 200
    second = client.post("/api/replays").json()
    assert not second["cached"] and second["id"] != first["id"]
    assert (
        client.get(second["manifest_url"]).json()["random_seed"] == data["random_seed"]
    )
    check_error(client.get("/api/replays/unknown/manifest"))


def test_concurrent_generation_is_rejected(client, renderer):
    sample_settings(client)
    started, release = threading.Event(), threading.Event()
    real = client.app.state.pipeline.generate

    def slow(config):
        started.set()
        assert release.wait(5)
        return real(config)

    with (
        patch.object(client.app.state.pipeline, "generate", side_effect=slow),
        ThreadPoolExecutor(1) as pool,
    ):
        future = pool.submit(client.post, "/api/replays")
        assert started.wait(5)
        try:
            response = client.post("/api/replays")
            check_error(response, "GENERATION_IN_PROGRESS")
            assert response.status_code == 409
        finally:
            release.set()
        assert future.result(timeout=5).status_code == 200


# Feature: sleep-replay-app, Property 2: Replay cache equivalence
@settings(max_examples=100)
@given(
    st.integers(0, 2**32 - 2),
    st.sampled_from(
        (
            "seed",
            "fingerprint",
            "version",
            "target",
            "sensitivity",
            "duration",
            "smoothing",
            "hysteresis",
        )
    ),
)
def test_cache_equivalence(seed, change):
    from dataclasses import replace
    from types import SimpleNamespace
    from unittest.mock import Mock
    from backend.domain.mapping import mapping_dict
    from backend.sonification.manifest import serialize_manifest

    source = fixture_manifest(seed)
    config = source.mapping_config
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        (root / "replay.wav").write_bytes(b"identical cached audio")
        (root / "replay.json").write_bytes(serialize_manifest(source))
        record = SimpleNamespace(
            wav_file="replay.wav",
            metadata=dict(
                manifest_file="replay.json",
                input_fingerprint=source.input_fingerprint,
                version=source.version,
                mapping_config=mapping_dict(config),
            ),
        )
        store = SimpleNamespace(list_replays=lambda: [record])
        cache = ReplayCache(root, store)
        renderer = Mock()

        def get(fingerprint, mapping):
            hit = cache.find(fingerprint, mapping)
            return cache.paths(hit)[0].read_bytes() if hit else renderer()

        assert (
            get(source.input_fingerprint, config) == (root / "replay.wav").read_bytes()
        )
        renderer.assert_not_called()
        fingerprint = source.input_fingerprint
        if change == "seed":
            config = replace(config, random_seed=seed + 1)
        elif change == "duration":
            config = replace(config, target_duration=120)
        elif change == "fingerprint":
            fingerprint = "b" * 64
        elif change == "version":
            record.metadata["version"] = "future"
        else:
            metrics = dict(config.metrics)
            values = {
                "target": {"target": Mapping_Target.none},
                "sensitivity": {"sensitivity": 0.8},
                "smoothing": {"smoothing_minutes": 30},
                "hysteresis": {"hysteresis": 0.5},
            }
            metrics["temperature"] = replace(metrics["temperature"], **values[change])
            config = replace(config, metrics=metrics)
        get(fingerprint, config)
        renderer.assert_called_once()
        assert cache_key(
            source.input_fingerprint, source.mapping_config, "future"
        ) != cache_key(source.input_fingerprint, source.mapping_config)


def test_offline_flow(client, renderer, monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("Outbound network is forbidden")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    sample_settings(client)
    response = client.post("/api/replays")
    assert response.status_code == 200
    for key in ("manifest_url", "audio_url"):
        assert client.get(response.json()[key]).status_code == 200


def test_missing_and_corrupt_cache_artifacts(client, renderer):
    sample_settings(client)
    first = client.post("/api/replays").json()
    record = client.app.state.pipeline.metadata_store.get_replay(first["id"])
    manifest = client.app.state.pipeline.data_dir / record.metadata["manifest_file"]
    manifest.write_text("corrupt")
    assert client.post("/api/replays").json()["cached"] is False
    second = client.post("/api/replays").json()
    record = client.app.state.pipeline.metadata_store.get_replay(second["id"])
    (client.app.state.pipeline.data_dir / record.wav_file).unlink()
    assert client.post("/api/replays").json()["cached"] is False


def test_api_restart_restores_settings_session_and_cache(tmp_path, renderer):
    with TestClient(
        create_app(tmp_path, display_timezone="America/New_York")
    ) as client:
        saved = sample_settings(client)
        saved["display_units"] = "metric"
        client.put("/api/settings", json=saved)
        replay = client.post("/api/replays").json()
        selected = client.get("/api/sessions").json()["selected_id"]
    with TestClient(
        create_app(tmp_path, display_timezone="America/New_York")
    ) as client:
        assert client.get("/api/settings").json() == saved
        assert client.get("/api/sessions").json()["selected_id"] == selected
        assert client.post("/api/replays").json() == {**replay, "cached": True}
        assert client.get(replay["audio_url"]).status_code == 200
    assert renderer.call_count == 1


def test_no_usable_data_and_display_zone_cache(client, renderer):
    client.post(
        "/api/sessions/manual",
        json={"start": "2024-03-01T22:00", "end": "2024-03-02T06:00"},
    )
    check_error(client.post("/api/replays"), "NO_USABLE_DATA")
    sample_settings(client)
    replay = client.post("/api/replays").json()
    state = client.app.state
    config = state.settings.get().to_mapping_config()
    from backend.processing.fingerprint import input_fingerprint

    fingerprint = input_fingerprint(state.pipeline.selected_session)
    assert (
        state.cache.find(fingerprint, config, "America/New_York").replay_id
        == replay["id"]
    )
    assert state.cache.find(fingerprint, config, "UTC") is None


def test_multiple_sessions_order_and_selection(client):
    client.post("/api/imports/sample")
    original = (ROOT / "sample_data/sleep-2024-03-01.json").read_text()
    later = (
        original.replace("2024-03-02", "2024-03-04")
        .replace("2024-03-01", "2024-03-03")
        .replace("synthetic-night", "another-night")
    )
    response = client.post(
        "/api/imports", files={"fitbit": ("sleep-2024-03-03.json", later.encode())}
    )
    assert response.status_code == 200
    rows = response.json()["sessions"]
    assert len(rows) == 2 and rows[0]["start_time"] > rows[1]["start_time"]
    assert response.json()["selected_id"] == rows[0]["key"]
    chosen = client.put("/api/sessions/selected", json={"id": rows[1]["key"]})
    assert chosen.json()["selected_id"] == rows[1]["key"]
