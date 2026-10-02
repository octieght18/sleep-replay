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
        ("sound_style", "other", "INVALID_MAPPING_CONFIG"),
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


def test_legacy_saved_settings_default_to_music_after_restart(tmp_path):
    original = DEFAULT_SETTINGS.to_dict()
    original.pop("sound_style")
    with MetadataStore(tmp_path) as store:
        store.set_setting("application_settings", original)
    with MetadataStore(tmp_path) as store:
        restored = SettingsService(store).get()
        assert restored.sound_style == "music"
        assert restored.to_dict() == {**original, "sound_style": "music"}
        assert restored.to_mapping_config().sound_style == "music"


def test_style_switch_creates_distinct_replay_and_cache_survives_restart(tmp_path):
    from backend.audio.renderer import render

    with TestClient(
        create_app(tmp_path, display_timezone="America/New_York")
    ) as client:
        data = sample_settings(client)
        with patch("backend.sonification.generation.render", wraps=render) as renderer:
            music = client.post("/api/replays").json()
            data["sound_style"] = "nature"
            assert client.put("/api/settings", json=data).status_code == 200
            nature = client.post("/api/replays").json()
            assert not nature["cached"] and nature["id"] != music["id"]
            document = client.get(nature["manifest_url"]).json()
            assert document["mapping_config"]["sound_style"] == "nature"
            assert document["sound_provenance"]["license"] == "CC0-1.0"
            assert document["version"] == "0.3.0"
            assert renderer.call_count == 2
            assert client.post("/api/replays").json()["cached"]
            audio = client.get(nature["audio_url"]).content
            from sonification_helpers import assert_audio, read_wav

            assert_audio(*read_wav(audio), 30)
            data["sound_style"] = "music"
            client.put("/api/settings", json=data)
            assert client.post("/api/replays").json()["id"] == music["id"]
            assert renderer.call_count == 2
    with TestClient(
        create_app(tmp_path, display_timezone="America/New_York")
    ) as restarted:
        data["sound_style"] = "nature"
        restarted.put("/api/settings", json=data)
        with patch("backend.sonification.generation.render") as renderer:
            cached = restarted.post("/api/replays").json()
            assert cached["cached"] and cached["id"] == nature["id"]
            assert restarted.get(cached["audio_url"]).content == audio
            renderer.assert_not_called()


setting_values = st.fixed_dictionaries(
    {
        "target_duration": st.sampled_from(TARGET_DURATIONS),
        "random_seed": st.integers(0, 2**32 - 1),
        "display_units": st.sampled_from(("imperial", "metric")),
        "sound_style": st.sampled_from(("music", "nature")),
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


@pytest.mark.parametrize(
    "content_type,body",
    [
        (None, b""),
        ("multipart/form-data; boundary=x", b"bad body"),
        ("application/json", b"{}"),
    ],
)
def test_malformed_upload_is_a_client_error_and_preserves_imports(
    client, content_type, body
):
    sample_settings(client)
    before = client.get("/api/sessions").json()
    headers = {} if content_type is None else {"Content-Type": content_type}
    response = client.post("/api/imports", headers=headers, content=body)
    assert response.status_code == 400
    check_error(response, "SOURCE_LOAD_FAILED")
    assert client.get("/api/sessions").json() == before
    parent = client.app.state.pipeline.data_dir / "uploads"
    assert not parent.exists() or not list(parent.iterdir())


def test_truncated_multipart_does_not_import_completed_parts(client):
    contents = (ROOT / "sample_data/sleep-2024-03-01.json").read_bytes()
    body = (
        b'--x\r\nContent-Disposition: form-data; name="fitbit"; '
        b'filename="sleep-2024-03-01.json"\r\n\r\n' + contents + b"\r\n--x\r\n"
    )
    response = client.post(
        "/api/imports",
        headers={"Content-Type": "multipart/form-data; boundary=x"},
        content=body,
    )
    assert response.status_code == 400
    check_error(response, "SOURCE_LOAD_FAILED")
    assert not client.get("/api/status").json()["has_imports"]
    assert not list((client.app.state.pipeline.data_dir / "uploads").iterdir())


def test_hrv_summary_change_invalidates_audio_cache(client):
    from backend.processing.fingerprint import input_fingerprint

    sleep = (ROOT / "sample_data/sleep-2024-03-01.json").read_bytes()
    data = DEFAULT_SETTINGS.to_dict()
    data.update(target_duration=30, sound_style="nature")
    client.put("/api/settings", json=data)
    assert (
        client.post(
            "/api/imports", files={"fitbit": ("sleep-2024-03-01.json", sleep)}
        ).status_code
        == 200
    )
    hashes, records, audio = [], [], []
    for value in (25, 75):
        summary = f"timestamp,rmssd\n2024-03-02,{value}\n".encode()
        response = client.post(
            "/api/imports",
            files=[
                (
                    "fitbit",
                    ("Daily Heart Rate Variability Summary - 2024-03.csv", summary),
                ),
            ],
        )
        assert response.status_code == 200
        assert client.app.state.pipeline.selected_session.session_hrv == (
            25 if value == 25 else 50
        )
        hashes.append(input_fingerprint(client.app.state.pipeline.selected_session))
        records.append(client.post("/api/replays").json())
        audio.append(client.get(records[-1]["audio_url"]).content)
    assert hashes[0] != hashes[1]
    assert records[0]["id"] != records[1]["id"]
    assert not records[1]["cached"]
    assert audio[0] != audio[1]
    assert client.post("/api/replays").json()["id"] == records[1]["id"]


@pytest.mark.parametrize("validator", ["etag", "last-modified"])
@pytest.mark.parametrize("range_,status", [("bytes=9999999-", 416), ("invalid", 400)])
def test_conditional_range_errors_are_structured(
    client, renderer, validator, range_, status
):
    sample_settings(client)
    url = client.post("/api/replays").json()["audio_url"]
    matching = client.head(url).headers[validator]
    response = client.get(url, headers={"Range": range_, "If-Range": matching})
    assert response.status_code == status
    check_error(response, "SOURCE_LOAD_FAILED")
    stale = client.get(url, headers={"Range": range_, "If-Range": '"stale"'})
    assert stale.status_code == 200


def test_oversized_suffix_and_mixed_satisfiable_ranges(client, renderer):
    sample_settings(client)
    url = client.post("/api/replays").json()["audio_url"]
    complete = client.get(url).content
    response = client.get(url, headers={"Range": f"bytes=-{len(complete) + 100}"})
    assert response.status_code == 206
    assert response.content == complete
    assert (
        response.headers["content-range"]
        == f"bytes 0-{len(complete) - 1}/{len(complete)}"
    )
    response = client.get(url, headers={"Range": "bytes=0-43,9999999-"})
    assert response.status_code == 206 and response.content == complete[:44]


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
            "style",
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
        elif change == "style":
            config = replace(config, sound_style="nature")
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
