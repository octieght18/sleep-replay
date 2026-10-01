"""Unit tests for backend/domain/timezones.py (Requirement 9.1-9.4, 9.10).

DST reference points used below (tzdata):
* Europe/Berlin 2024-03-31: 02:00 CET -> 03:00 CEST (skipped hour 02:00-03:00).
* Europe/Berlin 2024-10-27: 03:00 CEST -> 02:00 CET (repeated hour 02:00-03:00).
* America/New_York 2024-03-10 02:00 -> 03:00; 2024-11-03 02:00 -> 01:00.
* Australia/Lord_Howe 2024-10-06: 02:00 -> 02:30 (30-minute gap).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from backend.domain.errors import INVALID_TIMEZONE, NO_ACTION_REQUIRED, User_Error
from backend.domain.timezones import (
    DEFAULT_SOURCE_TIMEZONES,
    DISPLAY_TIMEZONE,
    FILE_TYPES,
    FITBIT_HEART_RATE,
    FITBIT_HRV_DETAILS,
    FITBIT_HRV_SUMMARY,
    FITBIT_SLEEP,
    FITBIT_STEPS,
    SENSORPUSH,
    DstAdjustmentCounter,
    Localized,
    localize,
    resolve_source_timezone,
    validate_iana,
    validate_overrides,
)

BERLIN = ZoneInfo("Europe/Berlin")
NEW_YORK = ZoneInfo("America/New_York")
UTC = timezone.utc


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


# ---------------------------------------------------------------------------
# validate_iana
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["UTC", "Europe/Berlin", "America/New_York", "Asia/Kolkata", "Etc/GMT+5"])
def test_validate_iana_accepts_valid_names(name: str) -> None:
    zone = validate_iana(name)
    assert isinstance(zone, ZoneInfo)
    assert zone.key == name


@pytest.mark.parametrize(
    "name",
    [
        "europe/berlin",  # wrong case (must not depend on file-system case sensitivity)
        "EUROPE/BERLIN",
        "utc",
        "America/new_york",
        "Mars/Olympus_Mons",
        "Europe/Berlin ",
        " Europe/Berlin",
        "Europe/Berlin/",
        "../../etc/passwd",
        "+02:00",
        "CEST",
        "",
        None,
        42,
    ],
)
def test_validate_iana_rejects_invalid_names(name: object) -> None:
    with pytest.raises(User_Error) as exc_info:
        validate_iana(name)
    err = exc_info.value
    assert err.code == INVALID_TIMEZONE
    assert repr(name) in err.description
    assert err.details["value"] == str(name)
    assert "file_type" not in err.details


def test_validate_iana_error_names_file_type() -> None:
    with pytest.raises(User_Error) as exc_info:
        validate_iana("Not/AZone", SENSORPUSH)
    err = exc_info.value
    assert err.code == INVALID_TIMEZONE
    assert "'Not/AZone'" in err.description
    assert SENSORPUSH in err.description
    assert err.details == {"value": "Not/AZone", "file_type": SENSORPUSH}
    assert err.action.strip()


# ---------------------------------------------------------------------------
# validate_overrides
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("overrides", [None, {}])
def test_validate_overrides_empty(overrides: dict[str, str] | None) -> None:
    assert validate_overrides(overrides) == {}


def test_validate_overrides_resolves_valid_values() -> None:
    result = validate_overrides({FITBIT_HEART_RATE: "Europe/Berlin", SENSORPUSH: "America/New_York"})
    assert result == {FITBIT_HEART_RATE: BERLIN, SENSORPUSH: NEW_YORK}


def test_validate_overrides_invalid_value_names_value_and_file_type() -> None:
    with pytest.raises(User_Error) as exc_info:
        validate_overrides({FITBIT_STEPS: "UTC", FITBIT_SLEEP: "europe/berlin"})
    err = exc_info.value
    assert err.code == INVALID_TIMEZONE
    assert err.details == {"value": "europe/berlin", "file_type": FITBIT_SLEEP}


def test_validate_overrides_reports_first_invalid_in_file_type_order() -> None:
    # Insertion order puts sensorpush first; FILE_TYPES order puts fitbit_sleep first.
    with pytest.raises(User_Error) as exc_info:
        validate_overrides({SENSORPUSH: "Bad/Two", FITBIT_SLEEP: "Bad/One"})
    assert exc_info.value.details["file_type"] == FITBIT_SLEEP
    assert exc_info.value.details["value"] == "Bad/One"


def test_validate_overrides_unknown_file_type() -> None:
    with pytest.raises(ValueError, match="fitbit_calories"):
        validate_overrides({"fitbit_calories": "UTC"})


# ---------------------------------------------------------------------------
# resolve_source_timezone
# ---------------------------------------------------------------------------


def test_default_source_timezones_cover_every_file_type() -> None:
    assert set(DEFAULT_SOURCE_TIMEZONES) == set(FILE_TYPES)


@pytest.mark.parametrize("file_type", [FITBIT_HEART_RATE, FITBIT_STEPS])
def test_fitbit_intraday_defaults_to_utc(file_type: str) -> None:
    assert resolve_source_timezone(file_type, None, BERLIN).key == "UTC"


@pytest.mark.parametrize("file_type", [FITBIT_SLEEP, FITBIT_HRV_DETAILS, FITBIT_HRV_SUMMARY, SENSORPUSH])
def test_other_file_types_default_to_display_timezone(file_type: str) -> None:
    assert DEFAULT_SOURCE_TIMEZONES[file_type] == DISPLAY_TIMEZONE
    assert resolve_source_timezone(file_type, None, BERLIN) is BERLIN
    assert resolve_source_timezone(file_type, {}, "America/New_York").key == "America/New_York"


def test_override_wins_over_default() -> None:
    overrides = {FITBIT_HEART_RATE: "Europe/Berlin", SENSORPUSH: "Asia/Tokyo"}
    assert resolve_source_timezone(FITBIT_HEART_RATE, overrides, NEW_YORK).key == "Europe/Berlin"
    assert resolve_source_timezone(SENSORPUSH, overrides, NEW_YORK).key == "Asia/Tokyo"
    # File types without an override keep their defaults.
    assert resolve_source_timezone(FITBIT_STEPS, overrides, NEW_YORK).key == "UTC"
    assert resolve_source_timezone(FITBIT_SLEEP, overrides, NEW_YORK) is NEW_YORK


def test_invalid_override_gives_invalid_timezone() -> None:
    with pytest.raises(User_Error) as exc_info:
        resolve_source_timezone(FITBIT_STEPS, {FITBIT_STEPS: "Utc"}, BERLIN)
    assert exc_info.value.code == INVALID_TIMEZONE
    assert exc_info.value.details == {"value": "Utc", "file_type": FITBIT_STEPS}


def test_invalid_display_timezone_string_gives_invalid_timezone() -> None:
    with pytest.raises(User_Error) as exc_info:
        resolve_source_timezone(SENSORPUSH, None, "Nowhere/Land")
    assert exc_info.value.code == INVALID_TIMEZONE


def test_unknown_file_type_raises_value_error() -> None:
    with pytest.raises(ValueError):
        resolve_source_timezone("garmin_sleep", None, BERLIN)


# ---------------------------------------------------------------------------
# localize
# ---------------------------------------------------------------------------


def test_localize_unambiguous_wall_time() -> None:
    result = localize(datetime(2024, 7, 1, 23, 15), BERLIN)
    assert result == Localized(datetime(2024, 7, 1, 23, 15, tzinfo=BERLIN), None)
    assert not result.adjusted
    assert result.value.astimezone(UTC) == utc(2024, 7, 1, 21, 15)


@pytest.mark.parametrize(
    ("zone", "naive", "expected_utc"),
    [
        # Repeated hour: earlier occurrence = pre-transition (summer) offset.
        ("Europe/Berlin", datetime(2024, 10, 27, 2, 30), utc(2024, 10, 27, 0, 30)),
        ("America/New_York", datetime(2024, 11, 3, 1, 30), utc(2024, 11, 3, 5, 30)),
        ("Australia/Sydney", datetime(2024, 4, 7, 2, 30), utc(2024, 4, 6, 15, 30)),
        # Europe/Dublin models winter time as negative DST in tzdata.
        ("Europe/Dublin", datetime(2024, 10, 27, 1, 30), utc(2024, 10, 27, 0, 30)),
    ],
)
def test_localize_ambiguous_resolves_to_earlier_instant(zone: str, naive: datetime, expected_utc: datetime) -> None:
    tz = ZoneInfo(zone)
    result = localize(naive, tz)
    assert result.adjustment == "ambiguous"
    assert result.adjusted
    assert result.value.astimezone(UTC) == expected_utc
    assert result.value.replace(tzinfo=None, fold=0) == naive
    # The later occurrence is exactly one hour after the chosen one.
    later = naive.replace(tzinfo=tz, fold=1 - result.value.fold).astimezone(UTC)
    assert later - expected_utc == timedelta(hours=1)


def test_localize_ambiguous_ignores_input_fold() -> None:
    naive = datetime(2024, 10, 27, 2, 30, fold=1)
    result = localize(naive, BERLIN)
    assert result.adjustment == "ambiguous"
    assert result.value.astimezone(UTC) == utc(2024, 10, 27, 0, 30)


@pytest.mark.parametrize(
    ("zone", "naive", "expected_wall", "expected_utc"),
    [
        # 02:30 in a 1-hour gap becomes 03:30 (Requirement 9.4 example).
        ("Europe/Berlin", datetime(2024, 3, 31, 2, 30), datetime(2024, 3, 31, 3, 30), utc(2024, 3, 31, 1, 30)),
        ("Europe/Berlin", datetime(2024, 3, 31, 2, 0), datetime(2024, 3, 31, 3, 0), utc(2024, 3, 31, 1, 0)),
        ("America/New_York", datetime(2024, 3, 10, 2, 15), datetime(2024, 3, 10, 3, 15), utc(2024, 3, 10, 7, 15)),
        # 30-minute gap: shifted forward by 30 minutes.
        ("Australia/Lord_Howe", datetime(2024, 10, 6, 2, 10), datetime(2024, 10, 6, 2, 40), utc(2024, 10, 5, 15, 40)),
    ],
)
def test_localize_nonexistent_shifts_forward_by_gap(
    zone: str, naive: datetime, expected_wall: datetime, expected_utc: datetime
) -> None:
    tz = ZoneInfo(zone)
    result = localize(naive, tz)
    assert result.adjustment == "nonexistent"
    assert result.adjusted
    assert result.value.tzinfo is tz
    assert result.value.replace(tzinfo=None) == expected_wall
    assert result.value.astimezone(UTC) == expected_utc


def test_localize_boundaries_of_transitions_are_not_adjusted() -> None:
    # Last minute before the gap and first minute after it.
    assert localize(datetime(2024, 3, 31, 1, 59), BERLIN).adjustment is None
    assert localize(datetime(2024, 3, 31, 3, 0), BERLIN).adjustment is None
    # First minute after the repeated hour.
    assert localize(datetime(2024, 10, 27, 3, 0), BERLIN).adjustment is None


def test_localize_in_utc_never_adjusts() -> None:
    for naive in (datetime(2024, 3, 31, 2, 30), datetime(2024, 10, 27, 2, 30)):
        result = localize(naive, ZoneInfo("UTC"))
        assert result.adjustment is None
        assert result.value.astimezone(UTC) == naive.replace(tzinfo=UTC)


@pytest.mark.parametrize(
    "aware",
    [
        datetime(2024, 10, 27, 2, 30, tzinfo=timezone(timedelta(hours=-5))),  # "-05:00"
        datetime(2024, 3, 31, 2, 30, tzinfo=UTC),  # "Z", would be nonexistent in Berlin
        datetime(2024, 10, 27, 2, 30, tzinfo=timezone(timedelta(hours=1))),  # "+01:00", ambiguous in Berlin
    ],
)
def test_localize_explicit_offset_is_kept(aware: datetime) -> None:
    result = localize(aware, BERLIN)
    assert result.adjustment is None
    assert result.value is aware
    assert result.value.utcoffset() == aware.utcoffset()


def test_explicit_offset_beats_override() -> None:
    source_tz = resolve_source_timezone(SENSORPUSH, {SENSORPUSH: "Asia/Tokyo"}, BERLIN)
    assert source_tz.key == "Asia/Tokyo"
    explicit = datetime(2024, 1, 15, 23, 0, tzinfo=timezone(timedelta(hours=-5)))
    assert localize(explicit, source_tz).value.astimezone(UTC) == utc(2024, 1, 16, 4, 0)
    # Offset-free values do use the override.
    naive = datetime(2024, 1, 15, 23, 0)
    assert localize(naive, source_tz).value.astimezone(UTC) == utc(2024, 1, 15, 14, 0)


# ---------------------------------------------------------------------------
# DstAdjustmentCounter
# ---------------------------------------------------------------------------


def test_counter_counts_adjustments_per_file_and_warns_once_per_file() -> None:
    counter = DstAdjustmentCounter()
    b = "sensorpush-b.csv"
    a = "sleep-2024-10-26.json"

    # File b: 2 ambiguous + 1 nonexistent + 2 normal.
    counter.localize(b, datetime(2024, 10, 27, 2, 10), BERLIN)
    counter.localize(b, datetime(2024, 10, 27, 2, 50), BERLIN)
    counter.localize(b, datetime(2024, 3, 31, 2, 30), BERLIN)
    counter.localize(b, datetime(2024, 10, 27, 4, 0), BERLIN)
    counter.localize(b, datetime(2024, 10, 27, 1, 0), BERLIN)
    # File a: 1 ambiguous.
    value = counter.localize(a, datetime(2024, 10, 27, 2, 30), BERLIN)
    assert value.astimezone(UTC) == utc(2024, 10, 27, 0, 30)
    # File c: nothing adjusted, explicit offsets are never counted.
    counter.localize("c.csv", datetime(2024, 10, 27, 2, 30, tzinfo=UTC), BERLIN)
    counter.localize("c.csv", datetime(2024, 7, 1, 12, 0), BERLIN)

    assert counter.count(b) == 3
    assert counter.count(a) == 1
    assert counter.count("c.csv") == 0
    assert counter.counts == {a: 1, b: 3}

    warnings = counter.warnings()
    assert [w.subject for w in warnings] == sorted([a, b]) == [b, a]  # one per affected file, sorted
    assert "3 timestamps in sensorpush-b.csv" in warnings[0].description
    assert "were adjusted" in warnings[0].description
    assert "1 timestamp in sleep-2024-10-26.json" in warnings[1].description
    assert "was adjusted" in warnings[1].description
    assert all(w.recommended_action == NO_ACTION_REQUIRED for w in warnings)


def test_counter_without_adjustments_has_no_warnings() -> None:
    counter = DstAdjustmentCounter()
    counter.localize("f.csv", datetime(2024, 7, 1, 12, 0), BERLIN)
    assert counter.counts == {}
    assert counter.warnings() == []


def test_counter_record_returns_value_and_counts_only_adjusted() -> None:
    counter = DstAdjustmentCounter()
    plain = localize(datetime(2024, 7, 1, 12, 0), BERLIN)
    adjusted = localize(datetime(2024, 3, 31, 2, 30), BERLIN)
    assert counter.record("f", plain) is plain.value
    assert counter.record("f", adjusted) is adjusted.value
    assert counter.count("f") == 1


def test_counter_counts_property_is_a_copy() -> None:
    counter = DstAdjustmentCounter()
    counter.localize("f", datetime(2024, 3, 31, 2, 30), BERLIN)
    counter.counts["f"] = 99
    assert counter.count("f") == 1
