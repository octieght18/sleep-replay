"""Property tests for backend.processing.compression (Requirement 11).

Each test is tagged with the compression property it verifies.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from fractions import Fraction
from zoneinfo import ZoneInfo

from hypothesis import given, settings
from hypothesis import strategies as st

from backend.domain.session import SleepSession
from backend.processing import compression as c

# ---------------------------------------------------------------------------
# Property 11 helpers
# ---------------------------------------------------------------------------

_P11_UTC = timezone.utc
_P11_US = timedelta(microseconds=1)
_P11_MAX_DURATION_US = 16 * 3600 * 1_000_000  # about 16 hours
_P11_TOLERANCE_S = Fraction(1, 1000)  # 1 millisecond (Requirement 11.4)

# Zones with and without DST, including negative-DST (Dublin), half-hour
# (Kolkata, St. John's) and southern-hemisphere (Sydney) rules.
_P11_ZONES = tuple(
    ZoneInfo(name)
    for name in (
        "UTC",
        "Europe/Berlin",
        "America/New_York",
        "Australia/Sydney",
        "Asia/Kolkata",
        "Europe/Dublin",
        "America/St_Johns",
    )
)

# UTC instants of DST transitions; sessions starting up to 16 h earlier cross them.
_P11_DST_TRANSITIONS_UTC = (
    datetime(2024, 3, 10, 7, 0, tzinfo=_P11_UTC),  # New York spring forward
    datetime(2024, 11, 3, 6, 0, tzinfo=_P11_UTC),  # New York fall back
    datetime(2024, 3, 31, 1, 0, tzinfo=_P11_UTC),  # Berlin / Dublin spring forward
    datetime(2024, 10, 27, 1, 0, tzinfo=_P11_UTC),  # Berlin / Dublin fall back
    datetime(2023, 10, 29, 1, 0, tzinfo=_P11_UTC),  # Berlin fall back
    datetime(2024, 4, 6, 16, 0, tzinfo=_P11_UTC),  # Sydney fall back
    datetime(2024, 10, 5, 16, 0, tzinfo=_P11_UTC),  # Sydney spring forward
)

_p11_zones = st.sampled_from(_P11_ZONES)

_p11_start_uniform = st.datetimes(
    min_value=datetime(2000, 1, 1),
    max_value=datetime(2035, 12, 31),
).map(lambda naive: naive.replace(tzinfo=_P11_UTC))

_p11_start_near_dst = st.builds(
    lambda transition, before_us: transition - timedelta(microseconds=before_us),
    st.sampled_from(_P11_DST_TRANSITIONS_UTC),
    st.integers(min_value=0, max_value=_P11_MAX_DURATION_US),
)


@st.composite
def _p11_scenario(draw: st.DrawFn):
    """A session (aware bounds in drawn zones), a Target_Duration, and in-session instants."""
    target = draw(st.sampled_from(c.ALLOWED_TARGET_DURATIONS))
    start_utc = draw(st.one_of(_p11_start_uniform, _p11_start_near_dst))
    duration_us = draw(st.integers(min_value=target * 1_000_000 + 1, max_value=_P11_MAX_DURATION_US))
    end_utc = start_utc + timedelta(microseconds=duration_us)
    # astimezone sets fold correctly, so repeated-hour wall times stay unambiguous.
    start = start_utc.astimezone(draw(_p11_zones))
    end = end_utc.astimezone(draw(_p11_zones))
    offsets = draw(st.lists(st.integers(min_value=0, max_value=duration_us), min_size=1, max_size=10))
    instants = [(start_utc + timedelta(microseconds=o)).astimezone(draw(_p11_zones)) for o in offsets]
    return SleepSession(start_time=start, end_time=end, source="fitbit"), target, instants


def _p11_utc_offset_us(t: datetime, start: datetime) -> int:
    """Exact UTC elapsed microseconds from ``start`` to ``t``, computed independently of the module."""
    return (t.astimezone(_P11_UTC) - start.astimezone(_P11_UTC)) // _P11_US


# ---------------------------------------------------------------------------
# Property 11
# ---------------------------------------------------------------------------


# Feature: sleep-replay-data-pipeline, Property 11: Night_Time to Replay_Time mapping accuracy
@settings(max_examples=200)
@given(_p11_scenario())
def test_property_11_night_time_to_replay_time_mapping_accuracy(scenario) -> None:
    """Replay_Time is within 1 ms of (t - start_time) / Compression_Ratio, with exact endpoints.

    **Validates: Requirements 11.4**
    """
    session, target, instants = scenario
    mapping = c.night_compression(session, target)

    duration_us = _p11_utc_offset_us(session.end_time, session.start_time)
    assert mapping.duration_microseconds == duration_us
    # Compression_Ratio as an exact rational: UTC elapsed seconds / Target_Duration.
    ratio = Fraction(duration_us, 1_000_000) / target

    # Endpoints map exactly, whatever zone they are expressed in.
    assert c.replay_time(mapping, session.start_time) == 0.0
    assert c.replay_time(mapping, session.end_time) == float(target)
    for zone in _P11_ZONES:
        assert c.replay_time(mapping, session.start_time.astimezone(zone)) == 0.0
        assert c.replay_time(mapping, session.end_time.astimezone(zone)) == float(target)

    for t in instants:
        replay = c.replay_time(mapping, t)
        expected = Fraction(_p11_utc_offset_us(t, session.start_time), 1_000_000) / ratio
        assert abs(Fraction(replay) - expected) <= _P11_TOLERANCE_S, (t, replay, float(expected))
        assert 0.0 <= replay <= float(target)
