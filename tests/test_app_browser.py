"""Optional real Chromium flows over both local servers and genuine audio."""

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

pytestmark = pytest.mark.browser
ROOT = Path(__file__).resolve().parents[1]
URL = "http://127.0.0.1:8734"


@pytest.fixture(scope="module")
def browser():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        executable = (
            os.environ.get("SLEEP_REPLAY_BROWSER")
            or shutil.which("chromium")
            or shutil.which("google-chrome")
        )
        with p.chromium.launch(
            executable_path=executable, args=["--no-sandbox"] if os.name != "nt" else []
        ) as browser:
            yield browser


@pytest.fixture
def page(browser, tmp_path):
    env = {
        **os.environ,
        "SLEEP_REPLAY_DATA_DIR": str(tmp_path / "browser-data"),
        "SLEEP_REPLAY_DISPLAY_TIMEZONE": "America/New_York",
    }
    for key in (
        "SLEEP_REPLAY_API_HOST",
        "SLEEP_REPLAY_FRONTEND_HOST",
        "SLEEP_REPLAY_BACKEND_URL",
        "SLEEP_REPLAY_FRONTEND_ORIGIN",
    ):
        env.pop(key, None)
    process = subprocess.Popen(
        [sys.executable, "-m", "backend.api.run"],
        cwd=ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 15
        while True:
            assert process.poll() is None, (
                "The test app failed to start; free ports 8734 and 8735"
            )
            try:
                with urllib.request.urlopen(URL + "/api/status", timeout=0.5):
                    break
            except OSError:
                assert time.monotonic() < deadline, (
                    "Backend did not respond within 15 seconds"
                )
                time.sleep(0.05)
        with browser.new_context(viewport={"width": 1280, "height": 720}) as context:
            page = context.new_page()
            errors = []
            requests = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on("request", lambda request: requests.append(request.url))
            page.goto(URL)
            page.wait_for_function(
                "() => !document.getElementById('sample-button').disabled"
            )
            yield page
            assert not errors
            assert all(url.startswith(URL) for url in requests), requests
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def sample(page):
    page.locator("#sample-button").click()
    page.get_by_role("button", name="Open replay", exact=True).wait_for()
    assert "Accepted:" in page.locator("#import-report").inner_text()
    page.get_by_role("button", name="Open replay", exact=True).click()


def replay(page):
    sample(page)
    page.locator('[data-view="settings"]').click()
    page.locator("#target-duration").select_option("30")
    page.wait_for_function(
        "() => document.getElementById('settings-status').textContent==='Saved locally.'"
    )
    page.locator('[data-view="main"]').click()
    page.locator("#generate-button").click()
    page.wait_for_function("() => !document.getElementById('play-button').disabled")
    page.wait_for_function(
        "() => document.getElementById('replay-audio').readyState>=2"
    )


def test_first_start_import_report_and_failure_recovery(page):
    assert page.locator("#import-view").is_visible()
    box = page.locator("#disclaimer").bounding_box()
    assert box["y"] >= 0 and box["y"] + box["height"] <= 720
    assert page.locator('[data-file-type="fitbit_heart_rate"]').input_value() == "UTC"
    page.locator("#fitbit-files").set_input_files(
        {"name": "wrong.exe", "mimeType": "application/octet-stream", "buffer": b"x"}
    )
    page.locator("#import-button").click()
    assert "wrong.exe" in page.locator("#import-error").inner_text()
    sample(page)
    assert page.locator("#play-button").is_disabled()
    assert page.locator(".event-marker").count() == 0


def test_settings_invalid_last_valid_restore_scope_restart(page):
    page.locator('[data-view="settings"]').click()
    assert page.locator("#mapping-controls select").count() == 6
    page.locator("#target-duration").select_option("30")
    page.locator("#random-seed").fill("123")
    page.locator("#display-units").select_option("metric")
    page.locator("#sound-style").select_option("nature")
    page.locator("#sensitivity-temperature").fill("0.7")
    page.wait_for_function(
        "() => document.getElementById('settings-status').textContent==='Saved locally.'"
    )
    page.locator("#sensitivity-temperature").fill("0.03")
    assert "0.05" in page.locator("#sensitivity-error-temperature").inner_text()
    saved = page.evaluate("fetch('/api/settings').then(r=>r.json())")
    assert saved["sensitivities"]["temperature"] == 0.7
    page.locator("#restore-defaults").click()
    page.wait_for_function(
        "() => document.getElementById('settings-status').textContent==='Saved locally.'"
    )
    saved = page.evaluate("fetch('/api/settings').then(r=>r.json())")
    assert (
        saved["target_duration"] == 30
        and saved["random_seed"] == 123
        and saved["display_units"] == "metric"
        and saved["sound_style"] == "nature"
    )
    assert saved["sensitivities"]["temperature"] == 0.3
    page.reload()
    page.wait_for_function("() => !document.getElementById('sample-button').disabled")
    page.locator('[data-view="settings"]').click()
    assert page.locator("#random-seed").input_value() == "123"
    assert page.locator("#display-units").input_value() == "metric"
    assert page.locator("#sound-style").input_value() == "nature"


def test_manifest_playback_keyboard_markers_end_and_units(page):
    replay(page)
    assert page.locator("#position-label").inner_text() == "00:00"
    assert page.evaluate("document.getElementById('replay-audio').paused")
    assert page.locator(".event-marker").count() > 0
    page.locator("#timeline").focus()
    page.keyboard.press("ArrowRight")
    assert page.locator("#position-label").inner_text() == "00:05"
    page.locator("#play-button").click()
    page.wait_for_timeout(400)
    assert not page.evaluate("document.getElementById('replay-audio').paused")
    assert (
        page.evaluate(
            "Math.abs(Number(document.getElementById('timeline').getAttribute('aria-valuenow'))-document.getElementById('replay-audio').currentTime)"
        )
        < 0.1
    )
    page.locator("#play-button").click()
    page.wait_for_timeout(150)
    assert page.evaluate("document.getElementById('replay-audio').paused")
    marker = page.locator(".event-marker").first
    marker.focus()
    assert (
        marker.get_attribute("aria-label")
        and marker.locator(".event-label").is_visible()
    )
    marker.click()
    page.locator("#timeline").focus()
    page.keyboard.press("End")
    assert page.locator("#position-label").inner_text() == "00:30"
    page.locator("#play-button").click()
    page.wait_for_timeout(300)
    assert page.evaluate("document.getElementById('replay-audio').currentTime") < 1
    page.locator("#play-button").click()
    page.locator('[data-view="settings"]').click()
    page.locator("#display-units").select_option("metric")
    page.locator('[data-view="main"]').click()
    page.wait_for_function(
        "() => document.getElementById('environment-values').textContent.includes('°C')"
    )


def test_nature_playback_license_style_switch_and_loaded_replay(page):
    sample(page)
    page.locator('[data-view="settings"]').click()
    page.locator("#target-duration").select_option("30")
    page.locator("#sound-style").select_option("nature")
    page.locator('[data-view="main"]').click()
    page.locator("#generate-button").click()
    page.wait_for_function(
        "() => document.getElementById('replay-audio').readyState>=2"
    )
    assert page.locator("#replay-style").inner_text() == "Nature · CC0 audio"
    nature_source = page.locator("#replay-audio").get_attribute("src")
    page.locator("#play-button").click()
    page.wait_for_timeout(200)
    assert not page.evaluate("document.getElementById('replay-audio').paused")
    page.locator("#play-button").click()
    page.locator('[data-view="settings"]').click()
    page.locator("#sound-style").select_option("music")
    page.locator('[data-view="main"]').click()
    assert page.locator("#replay-style").inner_text() == "Nature · CC0 audio"
    assert page.locator("#replay-audio").get_attribute("src") == nature_source
    page.locator("#generate-button").click()
    page.wait_for_function(
        "() => document.getElementById('replay-style').textContent==='Ambient music'"
    )
    assert page.locator("#replay-audio").get_attribute("src") != nature_source


def test_generation_failure_preserves_replay_and_progress(page):
    replay(page)
    source = page.locator("#replay-audio").get_attribute("src")
    page.locator("#timeline").focus()
    page.keyboard.press("ArrowRight")
    observed = []

    def fail(route):
        observed.append(page.locator("#generation-progress").is_visible())
        observed.append(page.locator("#generate-button").is_disabled())
        route.fulfill(
            status=500,
            content_type="application/json",
            body=json.dumps(
                dict(
                    code="RENDERING_FAILED",
                    description="Injected generation failure",
                    action="Retry.",
                    file_name=None,
                )
            ),
        )

    page.route("**/api/replays", fail)
    page.locator("#generate-button").click()
    page.locator("#main-error").wait_for()
    assert observed == [True, True]
    assert page.locator("#replay-audio").get_attribute("src") == source
    assert page.locator("#position-label").inner_text() == "00:05"
    assert page.locator("#play-button").is_enabled()
    assert page.locator("#generate-button").is_enabled()


def test_import_failure_keeps_files_and_overrides(page):
    path = ROOT / "sample_data/sleep-2024-03-01.json"
    page.locator("#fitbit-files").set_input_files(path)
    page.locator(".timezone-details summary").click()
    page.locator('[data-file-type="fitbit_sleep"]').fill("America/Chicago")
    observed = []

    def fail(route):
        observed.append(page.locator("#import-progress").is_visible())
        observed.append(page.locator("#import-button").is_disabled())
        route.fulfill(
            status=400,
            content_type="application/json",
            body=json.dumps(
                dict(
                    code="SOURCE_LOAD_FAILED",
                    description="Injected import failure",
                    action="Retry.",
                    file_name=path.name,
                )
            ),
        )

    page.route("**/api/imports", fail)
    page.locator("#import-button").click()
    page.locator("#import-error").wait_for()
    assert observed == [True, True]
    assert page.locator("#import-button").is_enabled()
    assert (
        page.locator('[data-file-type="fitbit_sleep"]').input_value()
        == "America/Chicago"
    )
    assert (
        page.evaluate("document.getElementById('fitbit-files').files[0].name")
        == path.name
    )


def test_no_session_manual_range_state(page):
    page.locator("#sensorpush-files").set_input_files(
        ROOT / "sample_data/sensorpush.csv"
    )
    page.locator("#import-button").click()
    page.locator("#manual-panel").wait_for()
    assert page.locator("#generate-button").is_disabled()
    page.locator("#manual-start").fill("2024-03-01T22:00")
    page.locator("#manual-end").fill("2024-03-02T06:00")
    page.locator("#manual-form button").click()
    page.wait_for_function("() => !document.getElementById('generate-button').disabled")
    assert not page.locator("#manual-panel").is_visible()


@pytest.mark.parametrize("older_failed", [False, True])
def test_settings_status_waits_for_the_latest_queued_save(page, older_failed):
    page.locator('[data-view="settings"]').click()
    pending = []

    def hold(route):
        if route.request.method == "PUT":
            pending.append(route)
            page.evaluate(f"window.heldSettings = {len(pending)}")
        else:
            route.continue_()

    page.route("**/api/settings", hold)
    page.locator("#target-duration").select_option("30")
    page.wait_for_function("() => window.heldSettings===1")
    assert len(pending) == 1
    page.locator("#random-seed").fill("42")
    pending[0].fulfill(
        status=500 if older_failed else 200,
        content_type="application/json",
        body=json.dumps(
            {
                "code": "SOURCE_LOAD_FAILED",
                "description": "Old save failed",
                "action": "Retry.",
            }
        )
        if older_failed
        else pending[0].request.post_data,
    )
    page.wait_for_function("() => window.heldSettings===2")
    assert len(pending) == 2
    assert page.locator("#settings-status").inner_text() == "Saving…"
    assert page.locator("#settings-error").is_hidden()
    pending[1].fulfill(
        status=200, content_type="application/json", body=pending[1].request.post_data
    )
    page.wait_for_function(
        "() => document.getElementById('settings-status').textContent==='Saved locally.'"
    )


@pytest.mark.parametrize("older_failed", [False, True])
def test_import_while_manifest_is_loading_discards_old_generation(page, older_failed):
    replay(page)
    pending = []

    def hold(route):
        pending.append((route, route.fetch()))
        page.evaluate("window.heldManifest = true")

    page.route("**/api/replays/*/manifest", hold)
    page.locator("#generate-button").click()
    page.wait_for_function("() => window.heldManifest===true")
    assert len(pending) == 1
    page.locator('[data-view="import"]').click()
    page.locator("#sample-button").click()
    page.wait_for_function("() => !document.getElementById('sample-button').disabled")
    page.get_by_role("button", name="Open replay", exact=True).click()
    route, response = pending[0]
    if older_failed:
        route.fulfill(
            status=500,
            content_type="application/json",
            body=json.dumps(
                {
                    "code": "SOURCE_LOAD_FAILED",
                    "description": "Old generation failed",
                    "action": "Retry.",
                }
            ),
        )
    else:
        route.fulfill(response=response)
    page.wait_for_function(
        "() => document.getElementById('generation-progress').hidden"
    )
    assert page.locator("#replay-audio").get_attribute("src") is None
    assert page.locator("#play-button").is_disabled()
    assert page.locator("#replay-style").is_hidden()
    assert page.locator("#generate-button").is_enabled()
    assert page.locator("#main-error").is_hidden()
