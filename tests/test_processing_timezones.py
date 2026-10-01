"""Unit tests for backend/processing/timezones.py (Requirement 9.5-9.8, 9.11).

``resolve_display_timezone`` is always called with an explicit ``environ`` and
``host_detector`` so the results don't depend on the machine running the tests.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from backend.domain.timezones import localize, validate_iana
from backend.processing import timezones as ptz
from backend.processing.timezones import (
    DISPLAY_TIMEZONE_ENV,
    UTC_ZONE,
    WINDOWS_TO_IANA,
    DisplayTimezone,
    add_elapsed_seconds,
    detect_host_timezone,
    elapsed_seconds,
    resolve_display_timezone,
    to_display,
    to_utc,
    utc_epoch_seconds,
    windows_zone_to_iana,
)

BERLIN = ZoneInfo("Europe/Berlin")
NEW_YORK = ZoneInfo("America/New_York")
UTC = timezone.utc
HOUR = 3600.0


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


def no_host() -> str | None:
    return None


def must_not_be_called() -> str | None:
    raise AssertionError("host timezone detection must not run when a Display_Timezone is configured")


# ---------------------------------------------------------------------------
# resolve_display_timezone
# ---------------------------------------------------------------------------


def test_configured_display_timezone_is_used() -> None:
    result = resolve_display_timezone(
        environ={DISPLAY_TIMEZONE_ENV: "Europe/Berlin"}, host_detector=must_not_be_called
    )
    assert result == DisplayTimezone(BERLIN, "config", None)
    assert result.name == "Europe/Berlin"


def test_configured_value_is_stripped() -> None:
    result = resolve_display_timezone(environ={DISPLAY_TIMEZONE_ENV: "  Asia/Tokyo\n"}, host_detector=no_host)
    assert result.source == "config"
    assert result.name == "Asia/Tokyo"


@pytest.mark.parametrize("value", ["Mars/Olympus_Mons", "europe/berlin", "UTC+2", " Central European Time "])
def test_invalid_configured_value_falls_back_to_utc_with_warning(value: str) -> None:
    # An invalid configured value must not fall through to the host timezone.
    result = resolve_display_timezone(environ={DISPLAY_TIMEZONE_ENV: value}, host_detector=must_not_be_called)
    assert result.zone == UTC_ZONE
    assert result.name == "UTC"
    assert result.source == "fallback"
    warning = result.warning
    assert warning is not None
    assert repr(value.strip()) in warning.description
    assert "not a valid IANA timezone" in warning.description
    assert "UTC" in warning.description
    assert warning.subject == "Display_Timezone"
    assert DISPLAY_TIMEZONE_ENV in warning.recommended_action


@pytest.mark.parametrize("environ", [{}, {DISPLAY_TIMEZONE_ENV: ""}, {DISPLAY_TIMEZONE_ENV: "   "}])
def test_unset_or_blank_config_uses_host_timezone(environ: dict[str, str]) -> None:
    result = resolve_display_timezone(environ=environ, host_detector=lambda: "America/New_York")
    assert result == DisplayTimezone(NEW_YORK, "host", None)


def test_unknown_host_timezone_falls_back_to_utc_with_warning() -> None:
    result = resolve_display_timezone(environ={}, host_detector=no_host)
    assert result.zone == UTC_ZONE
    assert result.source == "fallback"
    warning = result.warning
    assert warning is not None
    assert "No Display_Timezone is configured" in warning.description
    assert "host machine's timezone could not be determined" in warning.description
    assert warning.subject == "Display_Timezone"
    assert DISPLAY_TIMEZONE_ENV in warning.recommended_action
    assert "IANA" in warning.recommended_action


@pytest.mark.parametrize("host_name", ["", "Not/AZone", "europe/berlin"])
def test_invalid_host_timezone_falls_back_to_utc(host_name: str) -> None:
    result = resolve_display_timezone(environ={}, host_detector=lambda: host_name)
    assert result.zone == UTC_ZONE
    assert result.source == "fallback"
    assert result.warning is not None


def test_resolve_display_timezone_default_detector_reads_given_environ() -> None:
    # Without a host_detector, the TZ entry of the given environ is used.
    result = resolve_display_timezone(environ={"TZ": "Europe/Paris"})
    assert result.source == "host"
    assert result.name == "Europe/Paris"


# ---------------------------------------------------------------------------
# detect_host_timezone
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tz_value", "expected"),
    [
        ("Europe/Berlin", "Europe/Berlin"),
        (":America/New_York", "America/New_York"),
        ("  Asia/Tokyo ", "Asia/Tokyo"),
        ("/usr/share/zoneinfo/Asia/Tokyo", "Asia/Tokyo"),
        (":/usr/share/zoneinfo/posix/Europe/Paris", "Europe/Paris"),
        ("/usr/share/zoneinfo/right/Australia/Sydney", "Australia/Sydney"),
        (r"C:\tz\zoneinfo\Europe\Rome", "Europe/Rome"),
    ],
)
def test_detect_host_timezone_from_tz_env(tz_value: str, expected: str) -> None:
    assert detect_host_timezone({"TZ": tz_value}) == expected


@pytest.mark.parametrize("platform", ["win32", "linux"])
@pytest.mark.parametrize("tz_value", [None, "", "Nowhere/Land", "EST5EDT,M3.2.0,M11.1.0"])
def test_detect_host_timezone_falls_through_to_platform_detection(
    monkeypatch: pytest.MonkeyPatch, platform: str, tz_value: str | None
) -> None:
    monkeypatch.setattr(ptz.sys, "platform", platform)
    monkeypatch.setattr(ptz, "_from_windows", lambda: "Europe/Berlin")
    monkeypatch.setattr(ptz, "_from_posix_files", lambda: "America/Chicago")
    environ = {} if tz_value is None else {"TZ": tz_value}
    expected = "Europe/Berlin" if platform == "win32" else "America/Chicago"
    assert detect_host_timezone(environ) == expected


def test_detect_host_timezone_real_host_returns_valid_name_or_none() -> None:
    name = detect_host_timezone({})
    assert name is None or validate_iana(name).key == name


def test_posix_timezone_file_is_read(tmp_path: Path) -> None:
    tz_file = tmp_path / "timezone"
    tz_file.write_text("Europe/Vienna\n", encoding="utf-8")
    assert ptz._from_posix_files(tz_file, tmp_path / "missing-localtime") == "Europe/Vienna"


def test_posix_detection_returns_none_without_usable_files(tmp_path: Path) -> None:
    assert ptz._from_posix_files(tmp_path / "missing", tmp_path / "missing-localtime") is None
    bad = tmp_path / "timezone"
    bad.write_text("not a zone", encoding="utf-8")
    assert ptz._from_posix_files(bad, tmp_path / "missing-localtime") is None


# ---------------------------------------------------------------------------
# windows_zone_to_iana
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("windows_name", "expected"),
    [
        ("W. Europe Standard Time", "Europe/Berlin"),
        ("w. europe standard time", "Europe/Berlin"),
        ("  Eastern Standard Time ", "America/New_York"),
        ("Pacific Daylight Time", "America/Los_Angeles"),  # daylight display name
        ("GMT Daylight Time", "Europe/London"),
        ("UTC", "Etc/UTC"),
        ("Pacific Standard Time (Mexico)", "America/Tijuana"),
    ],
)
def test_windows_zone_to_iana_known_names(windows_name: str, expected: str) -> None:
    assert windows_zone_to_iana(windows_name) == expected


@pytest.mark.parametrize("windows_name", [None, "", "Atlantis Standard Time", "Europe/Berlin"])
def test_windows_zone_to_iana_unknown_names(windows_name: str | None) -> None:
    assert windows_zone_to_iana(windows_name) is None


def test_windows_mapping_targets_are_valid_iana_names() -> None:
    invalid = []
    for windows_name, iana in WINDOWS_TO_IANA.items():
        try:
            validate_iana(iana)
        except Exception:  # noqa: BLE001 - collect every bad entry
            invalid.append((windows_name, iana))
    assert invalid == []


# ---------------------------------------------------------------------------
# to_utc / to_display / utc_epoch_seconds
# ---------------------------------------------------------------------------


def test_to_utc_converts_aware_values_and_ignores_tz() -> None:
    aware = datetime(2024, 1, 15, 23, 0, tzinfo=timezone(timedelta(hours=-5)))
    result = to_utc(aware, BERLIN)
    assert result == utc(2024, 1, 16, 4, 0)
    assert result.tzinfo is UTC


def test_to_utc_localizes_naive_values_in_tz() -> None:
    assert to_utc(datetime(2024, 7, 1, 12, 0), BERLIN) == utc(2024, 7, 1, 10, 0)
    assert to_utc(datetime(2024, 1, 1, 12, 0), BERLIN) == utc(2024, 1, 1, 11, 0)


def test_to_utc_applies_dst_rules_for_naive_values() -> None:
    # Ambiguous -> earlier instant; nonexistent -> shifted forward by the gap.
    assert to_utc(datetime(2024, 10, 27, 2, 30), BERLIN) == utc(2024, 10, 27, 0, 30)
    assert to_utc(datetime(2024, 3, 31, 2, 30), BERLIN) == utc(2024, 3, 31, 1, 30)


def test_to_utc_naive_without_tz_raises() -> None:
    with pytest.raises(ValueError):
        to_utc(datetime(2024, 1, 1, 0, 0))


def test_to_display_uses_offset_in_effect_at_each_instant() -> None:
    # Two instants an hour apart both show 02:30 in Berlin during fall-back.
    first = to_display(utc(2024, 10, 27, 0, 30), BERLIN)
    second = to_display(utc(2024, 10, 27, 1, 30), BERLIN)
    assert first.replace(tzinfo=None, fold=0) == second.replace(tzinfo=None, fold=0) == datetime(2024, 10, 27, 2, 30)
    assert first.utcoffset() == timedelta(hours=2)
    assert second.utcoffset() == timedelta(hours=1)
    # A session crossing the transition: post-transition times carry the new offset.
    assert to_display(utc(2024, 10, 27, 5, 0), BERLIN).utcoffset() == timedelta(hours=1)
    assert to_display(utc(2024, 10, 26, 20, 0), BERLIN).utcoffset() == timedelta(hours=2)


def test_to_display_accepts_display_timezone() -> None:
    display = DisplayTimezone(NEW_YORK, "config")
    result = to_display(utc(2024, 7, 1, 12, 0), display)
    assert result.tzinfo is NEW_YORK
    assert result.replace(tzinfo=None) == datetime(2024, 7, 1, 8, 0)


def test_utc_is_independent_of_display_timezone() -> None:
    # Requirement 9.5: re-displaying in another zone doesn't change the UTC instant.
    instant = utc(2024, 10, 27, 1, 15)
    for zone in (BERLIN, NEW_YORK, ZoneInfo("Asia/Kolkata"), UTC_ZONE):
        assert to_utc(to_display(instant, zone)) == instant
        assert utc_epoch_seconds(to_display(instant, zone)) == utc_epoch_seconds(instant)


def test_utc_epoch_seconds() -> None:
    assert utc_epoch_seconds(utc(1970, 1, 1)) == 0.0
    assert utc_epoch_seconds(datetime(1970, 1, 1, 1, 0, tzinfo=BERLIN)) == 0.0
    assert utc_epoch_seconds(utc(2024, 1, 1)) == 1_704_067_200.0


# ---------------------------------------------------------------------------
# elapsed_seconds / add_elapsed_seconds (Requirement 9.8)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("zone", "start_day", "end_day", "expected_hours"),
    [
        ("Europe/Berlin", datetime(2024, 10, 26), datetime(2024, 10, 27), 9),  # fall-back
        ("Europe/Berlin", datetime(2024, 3, 30), datetime(2024, 3, 31), 7),  # spring-forward
        ("America/New_York", datetime(2024, 11, 2), datetime(2024, 11, 3), 9),
        ("America/New_York", datetime(2024, 3, 9), datetime(2024, 3, 10), 7),
        ("Europe/Berlin", datetime(2024, 7, 1), datetime(2024, 7, 2), 8),  # no transition
    ],
)
def test_22_to_06_session_duration_uses_utc_elapsed_time(
    zone: str, start_day: datetime, end_day: datetime, expected_hours: int
) -> None:
    tz = ZoneInfo(zone)
    start = localize(start_day.replace(hour=22), tz).value
    end = localize(end_day.replace(hour=6), tz).value
    assert elapsed_seconds(start, end) == expected_hours * HOUR
    assert elapsed_seconds(end, start) == -expected_hours * HOUR
    # Same result when the endpoints are given as naive wall times converted via to_utc.
    assert elapsed_seconds(to_utc(start_day.replace(hour=22), tz), to_utc(end_day.replace(hour=6), tz)) == (
        expected_hours * HOUR
    )


def test_elapsed_seconds_differs_from_naive_aware_subtraction_across_dst() -> None:
    # Python subtracts wall times for aware datetimes sharing a ZoneInfo; the
    # helper must not.
    start = datetime(2024, 10, 26, 22, 0, tzinfo=BERLIN)
    end = datetime(2024, 10, 27, 6, 0, tzinfo=BERLIN)
    assert (end - start).total_seconds() == 8 * HOUR
    assert elapsed_seconds(start, end) == 9 * HOUR


def test_elapsed_seconds_mixed_offsets() -> None:
    start = datetime(2024, 1, 1, 22, 0, tzinfo=timezone(timedelta(hours=-5)))
    end = utc(2024, 1, 2, 5, 30)
    assert elapsed_seconds(start, end) == 2.5 * HOUR


def test_add_elapsed_seconds_across_fall_back() -> None:
    start = localize(datetime(2024, 10, 26, 22, 0), BERLIN).value
    end = add_elapsed_seconds(start, 9 * HOUR)
    assert end.tzinfo is UTC
    assert end == utc(2024, 10, 27, 5, 0)
    assert to_display(end, BERLIN).replace(tzinfo=None) == datetime(2024, 10, 27, 6, 0)
    # Uniform UTC spacing: hourly steps land on the repeated 02:00 hour twice.
    walls = [to_display(add_elapsed_seconds(start, h * HOUR), BERLIN).hour for h in range(10)]
    assert walls == [22, 23, 0, 1, 2, 2, 3, 4, 5, 6]


def test_add_elapsed_seconds_across_spring_forward() -> None:
    start = localize(datetime(2024, 3, 30, 22, 0), BERLIN).value
    walls = [to_display(add_elapsed_seconds(start, h * HOUR), BERLIN).hour for h in range(8)]
    assert walls == [22, 23, 0, 1, 3, 4, 5, 6]
    assert elapsed_seconds(start, add_elapsed_seconds(start, 7 * HOUR)) == 7 * HOUR


def test_add_elapsed_seconds_fractional_and_negative() -> None:
    start = utc(2024, 1, 1, 0, 0)
    assert add_elapsed_seconds(start, 0.5) == start + timedelta(milliseconds=500)
    assert add_elapsed_seconds(start, -60) == utc(2023, 12, 31, 23, 59)
