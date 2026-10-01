"""Sanity checks for Session_Detector listing and selection (task 9.3).

Full selection tests are task 9.6.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from backend.domain.errors import INVALID_MANUAL_RANGE, NO_SLEEP_SESSION, User_Error
from backend.domain.session import SleepSession
from backend.domain.stages import Sleep_Stage, Stage_Segment
from backend.processing.session_detector import (
    Selection,
    auto_select,
    choose_session,
    dedup_and_merge,
    define_manual_session,
    list_sessions,
    resolve_selection,
)

BERLIN = ZoneInfo("Europe/Berlin")


def _session(start: datetime, hours: float, *, main: bool = False, log_id: str | None = None) -> SleepSession:
    end = start + timedelta(hours=hours)
    return SleepSession(
        start_time=start,
        end_time=end,
        source="fitbit",
        stages=(Stage_Segment(start, end, Sleep_Stage.light),),
        log_id=log_id,
        is_main_sleep=main,
    )


def _utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


def test_listing_is_latest_first_in_display_timezone() -> None:
    early = _session(_utc(2024, 3, 1, 21, 0), 7.5)
    late = _session(_utc(2024, 3, 2, 21, 0), 8, main=True)
    entries = list_sessions([early, late], BERLIN)
    assert [e.session for e in entries] == [late, early]
    assert entries[1].start_time.tzinfo == BERLIN
    assert entries[1].start_time.hour == 22
    assert (entries[1].duration_hours_part, entries[1].duration_minutes_part) == (7, 30)
    assert entries[1].stage_data_availability == "stages"
    assert list_sessions([late, early], BERLIN) == entries


def test_auto_select_prefers_main_sleep_then_longest_on_latest_date() -> None:
    nap = _session(_utc(2024, 3, 2, 12, 0), 1)
    main = _session(_utc(2024, 3, 1, 21, 0), 7, main=True)
    assert auto_select([nap, main], BERLIN) == main

    short = _session(_utc(2024, 3, 2, 12, 0), 1)
    long = _session(_utc(2024, 3, 1, 22, 0), 8)  # ends 2024-03-02 07:00 Berlin
    older = _session(_utc(2024, 2, 28, 12, 0), 20)
    assert auto_select([short, long, older], BERLIN) == long
    assert auto_select([], BERLIN) is None


def test_user_choice_manual_range_and_persistence_across_imports() -> None:
    first = _session(_utc(2024, 3, 1, 21, 0), 7, log_id="a")
    other = _session(_utc(2024, 3, 2, 21, 0), 7, main=True, log_id="b")
    chosen = choose_session([first, other], list_sessions([first], BERLIN)[0].key)
    assert chosen == Selection(first, "user")

    # A later import overlaps the chosen session: the merged one stays selected.
    extra = _session(_utc(2024, 3, 2, 3, 0), 3)
    merged = dedup_and_merge([first, other, extra]).sessions
    after = resolve_selection(merged, chosen, BERLIN)
    assert after.kind == "user"
    assert after.session.start_time == first.start_time
    assert after.session.end_time == extra.end_time

    manual = define_manual_session("2024-03-01 23:00", "2024-03-02T06:30", BERLIN)
    assert manual.kind == "manual"
    assert manual.session.is_main_sleep is False
    assert [s.stage for s in manual.session.stages] == [Sleep_Stage.unknown]
    assert manual.session.start_time.astimezone(timezone.utc) == _utc(2024, 3, 1, 22, 0)
    assert resolve_selection([], manual, BERLIN) == manual

    # With nothing sticky selected, automatic selection applies again.
    assert resolve_selection(merged, None, BERLIN) == Selection(other, "auto")


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (None, "2024-03-02 06:00"),
        ("not a time", "2024-03-02 06:00"),
        ("2024-03-01", "2024-03-02 06:00"),
        ("2024-03-02 06:00", "2024-03-02 06:00"),
        ("2024-03-01 06:00", "2024-03-02 06:01"),
    ],
)
def test_invalid_manual_range(start: object, end: object) -> None:
    with pytest.raises(User_Error) as info:
        define_manual_session(start, end, BERLIN)
    assert info.value.code == INVALID_MANUAL_RANGE


def test_exactly_24_hours_is_accepted() -> None:
    manual = define_manual_session("2024-03-01 06:00", "2024-03-02 06:00", BERLIN)
    assert manual.session.end_time - manual.session.start_time == timedelta(hours=24)


def test_no_candidates_without_manual_selection_gives_no_sleep_session() -> None:
    with pytest.raises(User_Error) as info:
        resolve_selection([], None, BERLIN)
    assert info.value.code == NO_SLEEP_SESSION
