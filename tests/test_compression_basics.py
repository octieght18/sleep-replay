"""Sanity checks for backend.processing.compression (task 12.1; Requirement 11).

Fuller example and property tests follow in tasks 12.2-12.4.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from backend.domain.errors import INVALID_TARGET_DURATION, SESSION_TOO_SHORT, User_Error
from backend.domain.session import SleepSession
from backend.processing import compression as c

UTC = timezone.utc
START = datetime(2024, 3, 1, 22, 0, tzinfo=UTC)


def _session(duration: timedelta, start: datetime = START) -> SleepSession:
    return SleepSession(start_time=start, end_time=start + duration, source="fitbit")


def test_allowed_durations() -> None:
    assert c.ALLOWED_TARGET_DURATIONS == (30, 120, 180, 300, 600)
    assert c.DEFAULT_TARGET_DURATION == 180


def test_select_target_duration_precedence() -> None:
    assert c.select_target_duration(300, {"target_duration": 600}) == 300
    assert c.select_target_duration(None, {"target_duration": 600}) == 600
    assert c.select_target_duration(None, {}) == 180
    assert c.select_target_duration() == 180


@pytest.mark.parametrize("bad", [0, 60, 181, 179.5, "180", True, float("nan")])
def test_select_target_duration_rejects_invalid(bad: object) -> None:
    with pytest.raises(User_Error) as info:
        c.select_target_duration(bad)
    err = info.value
    assert err.code == INVALID_TARGET_DURATION
    for s in ("30", "120", "180", "300", "600"):
        assert s in err.description


def test_invalid_config_value_does_not_fall_through() -> None:
    with pytest.raises(User_Error):
        c.select_target_duration(None, {"target_duration": 90})


def test_eight_hour_ratio_is_160() -> None:
    session = _session(timedelta(hours=8))
    assert c.compression_ratio(session, 180) == 160.0
    mapping = c.night_compression(session, 180)
    assert mapping.ratio_text() == "160.000"
    assert mapping.ratio_rounded() == 160.0


def test_ratio_uses_utc_elapsed_across_dst() -> None:
    berlin = ZoneInfo("Europe/Berlin")
    # 2023-10-29 fall-back in Berlin: 22:00 -> 06:00 local lasts 9 h.
    session = SleepSession(
        start_time=datetime(2023, 10, 28, 22, 0, tzinfo=berlin),
        end_time=datetime(2023, 10, 29, 6, 0, tzinfo=berlin),
        source="fitbit",
    )
    assert c.compression_ratio(session, 180) == pytest.approx(9 * 3600 / 180, abs=0)


def test_replay_time_endpoints_and_inverse() -> None:
    session = _session(timedelta(hours=7, minutes=13, seconds=7, microseconds=3))
    for target in c.ALLOWED_TARGET_DURATIONS:
        m = c.night_compression(session, target)
        assert c.replay_time(m, session.start_time) == 0.0
        assert c.replay_time(m, session.end_time) == float(target)
        assert c.night_time(m, 0.0) == session.start_time
        assert c.night_time(m, float(target)) == session.end_time
        t = session.start_time + timedelta(hours=3, microseconds=17)
        r = c.replay_time(m, t)
        expected = (t - session.start_time).total_seconds() / m.ratio
        assert abs(r - expected) <= 0.001
        assert c.night_time(m, r) == t


def test_replay_time_strictly_monotonic_at_microsecond_steps() -> None:
    session = _session(timedelta(hours=10))
    m = c.night_compression(session, 30)
    for base in (session.start_time, session.start_time + timedelta(hours=5), session.end_time - timedelta(microseconds=5)):
        values = [m.replay_time(base + timedelta(microseconds=i)) for i in range(5)]
        assert all(a < b for a, b in zip(values, values[1:]))


def test_replay_time_outside_session_raises() -> None:
    m = c.night_compression(_session(timedelta(hours=1)), 30)
    with pytest.raises(ValueError):
        m.replay_time(START - timedelta(microseconds=1))
    with pytest.raises(ValueError):
        m.night_time(31.0)


def test_session_too_short_lists_shorter_durations() -> None:
    session = _session(timedelta(seconds=150))
    with pytest.raises(User_Error) as info:
        c.compression_ratio(session, 180)
    err = info.value
    assert err.code == SESSION_TOO_SHORT
    assert err.details["shorter_durations"] == "30, 120"


def test_session_equal_to_target_is_too_short() -> None:
    with pytest.raises(User_Error) as info:
        c.compression_ratio(_session(timedelta(seconds=180)), 180)
    assert info.value.details["shorter_durations"] == "30, 120"


def test_session_too_short_for_any_duration() -> None:
    with pytest.raises(User_Error) as info:
        c.compression_ratio(_session(timedelta(seconds=30)), 30)
    err = info.value
    assert err.code == SESSION_TOO_SHORT
    assert "too short to replay" in err.description
    assert err.details["shorter_durations"] == ""


def test_too_short_message_keeps_fractional_seconds() -> None:
    with pytest.raises(User_Error) as info:
        c.compression_ratio(_session(timedelta(seconds=180, milliseconds=500)), 300)
    assert "3 min 0.5 s" in info.value.description
    assert info.value.details["shorter_durations"] == "30, 120, 180"


def test_select_target_duration_reads_config_attribute() -> None:
    class _Config:
        target_duration = 120

    assert c.select_target_duration(None, _Config()) == 120
