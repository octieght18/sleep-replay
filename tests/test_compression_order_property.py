"""Property test for Night_Time -> Replay_Time order preservation (task 12.3).

Feature: sleep-replay-data-pipeline, Property 12: Night_Time to Replay_Time mapping is strictly order-preserving

*For any* SleepSession and any pair of Night_Times t1 < t2 within it, the
Replay_Time computed for t1 is strictly less than the Replay_Time computed
for t2.

**Validates: Requirements 11.5**

Sessions are built from UTC instants and expressed in IANA zones, some of them
crossing a DST transition (including 30-minute and 45-minute-offset zones).
Pairs are placed near the session start, the middle, the end, the DST
transition, or anywhere, and are often exactly 1 us apart. The ordering of
t1 and t2 is always established on UTC instants, never on wall times.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from hypothesis import event, given, settings
from hypothesis import strategies as st

from backend.domain.session import SleepSession
from backend.processing.compression import ALLOWED_TARGET_DURATIONS, night_compression

UTC = timezone.utc
_US = 1_000_000
_ONE_US = timedelta(microseconds=1)

# Zones with DST: whole-hour (Berlin, New York, Sydney, Santiago), 30-minute
# DST (Lord Howe), and a 45-minute base offset (Chatham). Plus zones without DST.
_DST_ZONES = (
    "Europe/Berlin",
    "America/New_York",
    "Australia/Sydney",
    "America/Santiago",
    "Australia/Lord_Howe",
    "Pacific/Chatham",
)
_FIXED_ZONES = ("UTC", "Asia/Kolkata")
_ZONES = _DST_ZONES + _FIXED_ZONES

_SCAN_START = datetime(2023, 1, 1, tzinfo=UTC)
_SCAN_END = datetime(2025, 1, 1, tzinfo=UTC)
_RANGE_START = datetime(2023, 1, 1, tzinfo=UTC)
_RANGE_END = datetime(2024, 12, 31, tzinfo=UTC)

_MAX_SIDE_US = 12 * 3600 * _US  # up to 12 h on each side of a DST transition
_MAX_DURATION_US = 24 * 3600 * _US  # sessions up to 24 h
_NEAR_US = 10  # "near" an anchor: within 10 us


def _offset(zone: ZoneInfo, instant: datetime) -> timedelta | None:
    return instant.astimezone(zone).utcoffset()


def _find_transitions(zone: ZoneInfo) -> tuple[datetime, ...]:
    """UTC instants in [_SCAN_START, _SCAN_END) at which ``zone``'s UTC offset changes.

    Scans daily, then bisects to the second (tz transitions fall on whole
    seconds). Each returned instant is the first one carrying the new offset.
    """
    found: list[datetime] = []
    day = timedelta(days=1)
    cur = _SCAN_START
    while cur < _SCAN_END:
        nxt = cur + day
        if _offset(zone, cur) != _offset(zone, nxt):
            # Bisect on whole seconds after ``cur``: offset at lo is old, at hi is new.
            old = _offset(zone, cur)
            lo, hi = 0, 86_400
            while hi - lo > 1:
                mid = (lo + hi) // 2
                if _offset(zone, cur + timedelta(seconds=mid)) == old:
                    lo = mid
                else:
                    hi = mid
            found.append(cur + timedelta(seconds=hi))
        cur = nxt
    return tuple(found)


_TRANSITIONS: dict[str, tuple[datetime, ...]] = {key: _find_transitions(ZoneInfo(key)) for key in _ZONES}
for _key in _DST_ZONES:
    assert len(_TRANSITIONS[_key]) >= 2, f"no DST transitions found for {_key}; is tzdata installed?"


@dataclass(frozen=True)
class _Scenario:
    session: SleepSession
    zone_key: str
    target: int
    t1: datetime
    t2: datetime
    crosses_dst: bool
    anchor: str


@st.composite
def _scenarios(draw: st.DrawFn) -> _Scenario:
    zone_key = draw(st.sampled_from(_ZONES))
    zone = ZoneInfo(zone_key)
    target = draw(st.sampled_from(ALLOWED_TARGET_DURATIONS))
    min_duration_us = target * _US + 1  # the session must be longer than the Target_Duration

    transitions = _TRANSITIONS[zone_key]
    crosses_dst = bool(transitions) and draw(st.booleans())
    transition_offset_us: int | None = None
    if crosses_dst:
        transition = draw(st.sampled_from(transitions))
        before_us = draw(st.integers(1, _MAX_SIDE_US))
        after_us = draw(st.integers(max(1, min_duration_us - before_us), _MAX_SIDE_US))
        start_utc = transition - timedelta(microseconds=before_us)
        duration_us = before_us + after_us
        transition_offset_us = before_us
    else:
        start_utc = draw(
            st.datetimes(
                min_value=_RANGE_START.replace(tzinfo=None),
                max_value=_RANGE_END.replace(tzinfo=None),
                timezones=st.just(UTC),
            )
        )
        duration_us = draw(
            st.one_of(
                st.integers(min_duration_us, min_duration_us + 1_000),  # barely longer than target
                st.integers(min_duration_us, _MAX_DURATION_US),
            )
        )
    end_utc = start_utc + timedelta(microseconds=duration_us)

    gap_us = draw(st.one_of(st.just(1), st.integers(1, 1_000), st.integers(1, duration_us)))
    max_o1 = duration_us - gap_us
    anchors = ["start", "middle", "end", "anywhere"] + (["transition"] if crosses_dst else [])
    anchor = draw(st.sampled_from(anchors))
    if anchor == "start":
        o1 = draw(st.integers(0, min(_NEAR_US, max_o1)))
    elif anchor == "end":
        o1 = draw(st.integers(max(0, max_o1 - _NEAR_US), max_o1))
    elif anchor == "middle":
        mid = duration_us // 2
        hi = min(max_o1, mid + _NEAR_US)
        o1 = draw(st.integers(min(max(0, mid - _NEAR_US), hi), hi))
    elif anchor == "transition":
        assert transition_offset_us is not None
        lo = max(0, transition_offset_us - gap_us - _NEAR_US)
        hi = min(max_o1, transition_offset_us + _NEAR_US)
        o1 = draw(st.integers(lo, max(lo, hi)))
    else:
        o1 = draw(st.integers(0, max_o1))
    o1 = min(o1, max_o1)

    t1_utc = start_utc + timedelta(microseconds=o1)
    t2_utc = t1_utc + timedelta(microseconds=gap_us)

    # Express each instant in the session zone or in UTC, so the mapping has
    # to handle local wall times (with fold set by astimezone) and mixed tzinfo.
    def express(instant: datetime) -> datetime:
        return instant.astimezone(draw(st.sampled_from([zone, UTC])))

    session = SleepSession(
        start_time=express(start_utc),
        end_time=express(end_utc),
        source="fitbit",
    )
    return _Scenario(session, zone_key, target, express(t1_utc), express(t2_utc), crosses_dst, anchor)


# Feature: sleep-replay-data-pipeline, Property 12: Night_Time to Replay_Time mapping is strictly order-preserving
@settings(max_examples=300)
@given(_scenarios())
def test_replay_time_is_strictly_order_preserving(scenario: _Scenario) -> None:
    """t1 < t2 (as UTC instants) implies replay_time(t1) < replay_time(t2).

    **Validates: Requirements 11.5**
    """
    session, t1, t2 = scenario.session, scenario.t1, scenario.t2
    start_utc = session.start_time.astimezone(UTC)
    end_utc = session.end_time.astimezone(UTC)
    t1_utc, t2_utc = t1.astimezone(UTC), t2.astimezone(UTC)

    # Generator sanity: distinct in-session instants, ordered in UTC.
    assert start_utc <= t1_utc < t2_utc <= end_utc
    if scenario.crosses_dst:
        zone = ZoneInfo(scenario.zone_key)
        assert _offset(zone, start_utc) != _offset(zone, end_utc)

    event(f"crosses_dst={scenario.crosses_dst}")
    event(f"anchor={scenario.anchor}")
    event(f"gap_1us={t2_utc - t1_utc == _ONE_US}")

    mapping = night_compression(session, scenario.target)
    r1 = mapping.replay_time(t1)
    r2 = mapping.replay_time(t2)
    assert r1 < r2, (
        f"replay_time not strictly increasing: t1={t1_utc.isoformat()} -> {r1!r}, "
        f"t2={t2_utc.isoformat()} -> {r2!r} (gap {(t2_utc - t1_utc) // _ONE_US} us, "
        f"target {scenario.target} s, session {start_utc.isoformat()}..{end_utc.isoformat()}, "
        f"anchor {scenario.anchor})"
    )
