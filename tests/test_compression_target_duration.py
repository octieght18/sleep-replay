"""Unit tests for Target_Duration validation and selection (task 12.4).

Covers precedence, disallowed values, sessions not longer than the target,
error text and details, and Compression_Ratio rounding. Complements the
sanity checks in test_compression_basics.py.

Requirements: 11.1, 11.2, 11.3, 11.6, 11.7.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from fractions import Fraction

import pytest

from backend.domain.errors import INVALID_TARGET_DURATION, SESSION_TOO_SHORT, User_Error
from backend.domain.session import SleepSession
from backend.processing import compression as c

UTC = timezone.utc
START = datetime(2024, 3, 1, 22, 0, tzinfo=UTC)
ACCEPTED_TEXT = "30, 120, 180, 300, 600"
ALL_LABELS = (
    "30 seconds (30 s), 2 minutes (120 s), 3 minutes (180 s), "
    "5 minutes (300 s) or 10 minutes (600 s)"
)


def _mapping(duration: timedelta, target: int) -> c.Night_Compression:
    return c.Night_Compression(START, START + duration, target)


def _too_short(duration: timedelta, target: int) -> User_Error:
    with pytest.raises(User_Error) as info:
        _mapping(duration, target)
    assert info.value.code == SESSION_TOO_SHORT
    return info.value


def _invalid(value: object) -> User_Error:
    with pytest.raises(User_Error) as info:
        c.validate_target_duration(value)
    assert info.value.code == INVALID_TARGET_DURATION
    return info.value


# ---------------------------------------------------------------------------
# validate_target_duration (Req 11.1, 11.7)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("seconds", [30, 120, 180, 300, 600])
def test_validate_accepts_each_offered_value(seconds: int) -> None:
    assert c.validate_target_duration(seconds) == seconds


@pytest.mark.parametrize("value", [300.0, Fraction(600, 1)])
def test_validate_accepts_integral_reals_and_returns_int(value: object) -> None:
    result = c.validate_target_duration(value)
    assert result == int(value)  # type: ignore[call-overload]
    assert type(result) is int


@pytest.mark.parametrize(
    "value",
    [-30, 1, 29, 31, 599, 601, 3600, 30.000001, float("inf"), float("-inf"), None, "30", [180], False],
)
def test_validate_rejects_disallowed_values(value: object) -> None:
    _invalid(value)


def test_invalid_target_error_text_and_details() -> None:
    err = _invalid(90)
    assert err.description == (
        f"The Target_Duration 90 is not supported. The accepted values are {ALL_LABELS}."
    )
    assert err.action == f"Choose one of the accepted Target_Durations in seconds: {ACCEPTED_TEXT}."
    assert dict(err.details) == {"value": "90", "accepted_values": ACCEPTED_TEXT}
    assert err.file_name is None


def test_invalid_target_string_value_is_quoted() -> None:
    err = _invalid("3 minutes")
    assert "'3 minutes'" in err.description
    assert err.details["value"] == "'3 minutes'"


def test_invalid_target_error_is_plain_language() -> None:
    err = _invalid(45.5)
    text = str(err)
    assert "Traceback" not in text
    assert ".py" not in text
    assert "TypeError" not in text and "ValueError" not in text
    assert err.to_dict()["code"] == INVALID_TARGET_DURATION


# ---------------------------------------------------------------------------
# select_target_duration (Req 11.2, 11.7)
# ---------------------------------------------------------------------------


def test_request_wins_even_when_config_is_invalid() -> None:
    # The config value is never consulted when the request sets one.
    assert c.select_target_duration(30, {"target_duration": 999}) == 30


def test_invalid_request_does_not_fall_through_to_config() -> None:
    with pytest.raises(User_Error) as info:
        c.select_target_duration(60, {"target_duration": 300})
    assert info.value.code == INVALID_TARGET_DURATION
    assert info.value.details["value"] == "60"


@pytest.mark.parametrize("falsy", [0, 0.0])
def test_falsy_request_is_validated_not_treated_as_unset(falsy: object) -> None:
    with pytest.raises(User_Error):
        c.select_target_duration(falsy, {"target_duration": 300})


def test_falsy_config_value_is_validated_not_defaulted() -> None:
    with pytest.raises(User_Error):
        c.select_target_duration(None, {"target_duration": 0})


@pytest.mark.parametrize(
    "config",
    [None, {"target_duration": None}, {"other": 30}, object()],
    ids=["none", "mapping-none", "mapping-missing", "object-missing"],
)
def test_unset_config_uses_three_minute_default(config: object) -> None:
    assert c.select_target_duration(None, config) == 180


def test_config_object_with_none_attribute_uses_default() -> None:
    class _Config:
        target_duration = None

    assert c.select_target_duration(None, _Config()) == 180


def test_select_normalizes_integral_float_request() -> None:
    result = c.select_target_duration(600.0)
    assert result == 600 and type(result) is int


def test_rejection_leaves_active_selection_unchanged() -> None:
    config = {"target_duration": 120}
    assert c.select_target_duration(None, config) == 120
    with pytest.raises(User_Error):
        c.select_target_duration(240, config)
    assert config == {"target_duration": 120}
    assert c.select_target_duration(None, config) == 120


# ---------------------------------------------------------------------------
# SESSION_TOO_SHORT (Req 11.6)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("target", [30, 120, 180, 300, 600])
def test_session_equal_to_target_rejected_one_microsecond_longer_accepted(target: int) -> None:
    _too_short(timedelta(seconds=target), target)
    m = _mapping(timedelta(seconds=target, microseconds=1), target)
    assert m.ratio > 1.0


def test_zero_length_session_is_too_short() -> None:
    err = _too_short(timedelta(0), 30)
    assert "lasts 0 s" in err.description
    assert "too short to replay" in err.description


def test_invalid_target_checked_before_session_length() -> None:
    with pytest.raises(User_Error) as info:
        _mapping(timedelta(seconds=1), 90)
    assert info.value.code == INVALID_TARGET_DURATION


def test_too_short_error_text_with_shorter_values() -> None:
    err = _too_short(timedelta(minutes=2, seconds=10), 180)
    assert err.description == (
        "The sleep session lasts 2 min 10 s, which is not longer than the selected "
        "Target_Duration of 3 minutes. Target_Durations shorter than the session: "
        "30 seconds (30 s) or 2 minutes (120 s)."
    )
    assert err.action == "Choose a shorter Target_Duration: 30 seconds (30 s) or 2 minutes (120 s)."
    assert dict(err.details) == {
        "session_duration_s": "130",
        "target_duration_s": "180",
        "shorter_durations": "30, 120",
    }


def test_too_short_with_single_shorter_value() -> None:
    err = _too_short(timedelta(seconds=45, milliseconds=500), 120)
    assert "lasts 45.5 s" in err.description
    assert err.action == "Choose a shorter Target_Duration: 30 seconds (30 s)."
    assert err.details["shorter_durations"] == "30"
    assert err.details["session_duration_s"] == "45.5"


def test_too_short_at_ten_minutes_lists_four_shorter_values() -> None:
    err = _too_short(timedelta(minutes=10), 600)
    assert err.details["shorter_durations"] == "30, 120, 180, 300"
    assert "Target_Duration of 10 minutes" in err.description
    assert "5 minutes (300 s)" in err.action


def test_too_short_for_any_offered_value_text() -> None:
    err = _too_short(timedelta(seconds=12), 180)
    assert err.description == (
        "The sleep session lasts 12 s, which is too short to replay: it is not longer "
        "than any offered Target_Duration."
    )
    assert err.action == "Select or enter a sleep session longer than 30 seconds."
    assert dict(err.details) == {
        "session_duration_s": "12",
        "target_duration_s": "180",
        "shorter_durations": "",
    }


def test_too_short_message_formats_hours() -> None:
    # Not reachable with offered targets (max 600 s), but the formatter must be exact.
    assert c._format_elapsed(3600 * 1_000_000 + 61 * 1_000_000) == "1 h 1 min 1 s"
    assert c._format_elapsed(2 * 3600 * 1_000_000) == "2 h"


def test_compression_ratio_raises_too_short_for_sleep_session() -> None:
    session = SleepSession(start_time=START, end_time=START + timedelta(minutes=5), source="fitbit")
    with pytest.raises(User_Error) as info:
        c.compression_ratio(session, 300)
    assert info.value.code == SESSION_TOO_SHORT
    assert c.compression_ratio(session, 180) == pytest.approx(300 / 180)


# ---------------------------------------------------------------------------
# offered_durations_shorter_than / format_target_duration
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("duration_s", "expected"),
    [
        (0, ()),
        (30, ()),
        (30.000001, (30,)),
        (180, (30, 120)),
        (180.5, (30, 120, 180)),
        (600, (30, 120, 180, 300)),
        (8 * 3600, (30, 120, 180, 300, 600)),
    ],
)
def test_offered_durations_shorter_than(duration_s: float, expected: tuple[int, ...]) -> None:
    assert c.offered_durations_shorter_than(duration_s) == expected


@pytest.mark.parametrize(
    ("seconds", "label"),
    [(30, "30 seconds"), (120, "2 minutes"), (180, "3 minutes"), (300, "5 minutes"), (600, "10 minutes")],
)
def test_format_target_duration_labels(seconds: int, label: str) -> None:
    assert c.format_target_duration(seconds) == label


# ---------------------------------------------------------------------------
# Compression_Ratio rounding (Req 11.3)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("target", "text"),
    [(30, "960.000"), (120, "240.000"), (300, "96.000"), (600, "48.000")],
)
def test_eight_hour_ratio_text_for_other_targets(target: int, text: str) -> None:
    m = _mapping(timedelta(hours=8), target)
    assert m.ratio_text() == text
    assert m.ratio_exact == Fraction(8 * 3600, target)


def test_non_terminating_ratio_rounds_to_three_decimals() -> None:
    # 7 h 13 min 6 s = 25986 s; / 180 = 144.3666...
    m = _mapping(timedelta(hours=7, minutes=13, seconds=6), 180)
    assert m.ratio_exact == Fraction(25986, 180)
    assert m.ratio_text() == "144.367"
    assert m.ratio_rounded() == 144.367
    assert m.ratio_text(5) == "144.36667"
    assert m.ratio_rounded(5) == 144.36667


def test_ratio_rounding_keeps_at_least_three_decimals() -> None:
    m = _mapping(timedelta(hours=8), 180)
    assert m.ratio_text(4) == "160.0000"
    assert c.RATIO_DECIMALS == 3


@pytest.mark.parametrize("decimals", [0, 1, 2])
def test_ratio_rounding_rejects_fewer_than_three_decimals(decimals: int) -> None:
    m = _mapping(timedelta(hours=8), 180)
    with pytest.raises(ValueError):
        m.ratio_rounded(decimals)
    with pytest.raises(ValueError):
        m.ratio_text(decimals)


def test_ratio_uses_microsecond_duration() -> None:
    m = _mapping(timedelta(hours=1, microseconds=1), 30)
    assert m.duration_microseconds == 3600 * 1_000_000 + 1
    assert m.ratio_exact == Fraction(3600 * 1_000_000 + 1, 30 * 1_000_000)
    assert m.ratio_text() == "120.000"
    assert m.ratio == float(m.ratio_exact)
