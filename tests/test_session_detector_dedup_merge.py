"""Sanity tests for Session_Detector dedup and merge (task 9.1, Req 8.5, 8.6)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from backend.domain.errors import Import_Report
from backend.domain.session import SleepSession
from backend.domain.stages import Sleep_Stage, Stage_Segment
from backend.processing.session_detector import (
    dedup_and_merge,
    deduplicate_candidates,
    merge_overlapping_candidates,
)

UTC = timezone.utc
T0 = datetime(2024, 3, 1, 22, 0, tzinfo=UTC)


def h(hours: float) -> datetime:
    return T0 + timedelta(hours=hours)


def session(start, end, stage=Sleep_Stage.light, **kw) -> SleepSession:
    return SleepSession(
        start_time=start,
        end_time=end,
        source="fitbit",
        stages=(Stage_Segment(start, end, stage),),
        **kw,
    )


def test_dedup_shared_log_id_prefers_main_sleep():
    a = session(h(0), h(8), log_id="1", source_file="b.json")
    b = session(h(1), h(7), log_id="1", is_main_sleep=True, source_file="c.json")
    kept, discarded = deduplicate_candidates([a, b])
    assert kept == [b]
    assert discarded == 1


def test_dedup_identical_bounds_prefers_stages_then_file_name():
    classic = session(h(0), h(8), stage=Sleep_Stage.asleep, source_file="a.json")
    staged_z = session(h(0), h(8), source_file="z.json")
    staged_b = session(h(0), h(8), source_file="b.json")
    kept, discarded = deduplicate_candidates([classic, staged_z, staged_b])
    assert kept == [staged_b]
    assert discarded == 2


def test_dedup_bounds_compared_as_utc_instants():
    local = ZoneInfo("America/New_York")
    a = session(h(0), h(8), source_file="a.json")
    b = session(h(0).astimezone(local), h(8).astimezone(local), source_file="b.json")
    kept, discarded = deduplicate_candidates([b, a])
    assert kept == [a] and discarded == 1


def test_touching_sessions_do_not_merge():
    a = session(h(0), h(4))
    b = session(h(4), h(8))
    sessions, warnings = merge_overlapping_candidates([b, a])
    assert sessions == [a, b]
    assert warnings == []


def test_merge_uses_main_sleep_stages_in_overlap():
    main = session(h(2), h(6), stage=Sleep_Stage.deep, is_main_sleep=True, log_id="m")
    other = session(h(0), h(8), stage=Sleep_Stage.light, log_id="o")
    report = Import_Report()
    result = dedup_and_merge([other, main], report=report)
    (merged,) = result.sessions
    assert (merged.start_time, merged.end_time) == (h(0), h(8))
    assert merged.is_main_sleep
    assert [(s.start_time, s.end_time, s.stage) for s in merged.stages] == [
        (h(0), h(2), Sleep_Stage.light),
        (h(2), h(6), Sleep_Stage.deep),
        (h(6), h(8), Sleep_Stage.light),
    ]
    assert len(report.warnings) == 1
    assert h(0).isoformat() in report.warnings[0].description
    assert h(8).isoformat() in report.warnings[0].description


def test_merge_without_main_sleep_prefers_longer_then_earlier():
    longer = session(h(0), h(5), stage=Sleep_Stage.light)
    shorter = session(h(4), h(6), stage=Sleep_Stage.rem)
    (merged,) = dedup_and_merge([shorter, longer]).sessions
    assert [(s.start_time, s.end_time, s.stage) for s in merged.stages] == [
        (h(0), h(5), Sleep_Stage.light),
        (h(5), h(6), Sleep_Stage.rem),
    ]

    early = session(h(0), h(3), stage=Sleep_Stage.light)
    late = session(h(2), h(5), stage=Sleep_Stage.rem)
    (merged,) = dedup_and_merge([late, early]).sessions
    assert [(s.start_time, s.end_time, s.stage) for s in merged.stages] == [
        (h(0), h(3), Sleep_Stage.light),
        (h(3), h(5), Sleep_Stage.rem),
    ]


def test_chain_merges_to_fixpoint_and_is_order_independent():
    a = session(h(0), h(3), log_id="a")
    b = session(h(2), h(5), log_id="b")
    c = session(h(4.5), h(9), log_id="c", is_main_sleep=True)
    d = session(h(9), h(10), log_id="d")  # touches c only
    dup = session(h(0), h(3), log_id="x", source_file="z.json")  # same bounds as a
    first = dedup_and_merge([a, b, c, d, dup])
    assert first.discarded_duplicates == 1
    assert [(s.start_time, s.end_time) for s in first.sessions] == [(h(0), h(9)), (h(9), h(10))]
    assert first.sessions[0].is_main_sleep
    again = dedup_and_merge(first.sessions)
    assert again.sessions == first.sessions
    assert again.discarded_duplicates == 0 and again.warnings == ()
    reordered = dedup_and_merge([d, dup, c, b, a])
    assert reordered.sessions == first.sessions
