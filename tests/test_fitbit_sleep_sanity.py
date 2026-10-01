"""Sanity checks for backend.ingestion.fitbit.sleep (task 7.3).

Full unit and property tests come in tasks 7.7 and 7.9.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from backend.domain.errors import Import_Report
from backend.domain.stages import Sleep_Stage
from backend.domain.timezones import DstAdjustmentCounter
from backend.ingestion.fitbit.sleep import parse_sleep_json

NY = ZoneInfo("America/New_York")
FILE = "sleep-2024-01-02.json"


def _utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


def _parse(payload, tz=NY):
    report = Import_Report()
    data = payload if isinstance(payload, (bytes, str)) else json.dumps(payload)
    return parse_sleep_json(data, FILE, tz, report, DstAdjustmentCounter()), report


def test_stages_log_with_short_data_split_and_brief_awakening():
    log = {
        "logId": 123,
        "startTime": "2024-01-01T23:00:00.000",
        "endTime": "2024-01-02T01:00:00.000",
        "type": "stages",
        "isMainSleep": True,
        "levels": {
            "data": [
                {"dateTime": "2024-01-01T23:00:00.000", "level": "light", "seconds": 3600},
                {"dateTime": "2024-01-02T00:00:00.000", "level": "deep", "seconds": 3600},
            ],
            "shortData": [
                {"dateTime": "2024-01-01T23:30:00.000", "level": "wake", "seconds": 60},
            ],
        },
    }
    sessions, report = _parse([log])
    assert len(sessions) == 1
    s = sessions[0]
    assert s.log_id == "123" and s.is_main_sleep is True
    assert s.source == "fitbit" and s.source_file == FILE
    assert s.start_time == _utc(2024, 1, 2, 4) and s.end_time == _utc(2024, 1, 2, 6)
    assert [(x.stage, x.is_brief_awakening) for x in s.stages] == [
        (Sleep_Stage.light, False),
        (Sleep_Stage.awake, True),
        (Sleep_Stage.light, False),
        (Sleep_Stage.deep, False),
    ]
    assert s.stages[1].start_time == _utc(2024, 1, 2, 4, 30)
    assert s.stages[1].end_time == _utc(2024, 1, 2, 4, 31)
    assert report.warnings == [] and report.skipped_value_count == 0


def test_classic_log_unknown_level_and_invalid_levels_entries():
    log = {
        "logId": 7,
        "startTime": "2024-01-01T23:00:00",
        "endTime": "2024-01-02T00:00:00",
        "type": "classic",
        "isMainSleep": "yes",
        "levels": {
            "data": [
                {"dateTime": "2024-01-01T23:00:00", "level": "asleep", "seconds": 1200},
                {"dateTime": "2024-01-01T23:20:00", "level": "Restless", "seconds": 600},
                {"dateTime": "2024-01-01T23:30:00", "level": "dozing", "seconds": 300},
                {"dateTime": "2024-01-01T23:35:00", "level": "dozing", "seconds": 300},
                {"dateTime": "2024-01-01T23:40:00", "level": "awake", "seconds": 0},
                {"dateTime": "bad", "level": "awake", "seconds": 60},
            ],
        },
    }
    sessions, report = _parse([log])
    (s,) = sessions
    assert s.is_main_sleep is False
    assert [x.stage for x in s.stages] == [
        Sleep_Stage.asleep,
        Sleep_Stage.restless,
        Sleep_Stage.unknown,
        Sleep_Stage.unknown,
    ]
    assert report.skipped_value_count == 2
    unknown_warnings = [w for w in report.warnings if "dozing" in w.description]
    assert len(unknown_warnings) == 1 and "2 stage segments" in unknown_warnings[0].description


def test_missing_levels_gives_single_unknown_segment_and_warning():
    sessions, report = _parse(
        [{"logId": 1, "startTime": "2024-01-01T23:00:00", "endTime": "2024-01-02T07:00:00"}]
    )
    (s,) = sessions
    assert len(s.stages) == 1 and s.stages[0].stage is Sleep_Stage.unknown
    assert (s.stages[0].start_time, s.stages[0].end_time) == (s.start_time, s.end_time)
    assert len(report.warnings) == 1 and "unavailable" in report.warnings[0].description


def test_invalid_entries_are_skipped_and_the_rest_imported():
    good = {"logId": 3, "startTime": "2024-01-01T23:00:00", "endTime": "2024-01-02T07:00:00", "levels": {"data": []}}
    sessions, report = _parse(
        [
            {"logId": 1, "endTime": "2024-01-02T07:00:00"},
            {"logId": 2, "startTime": "2024-01-02T07:00:00", "endTime": "2024-01-02T07:00:00"},
            "not an object",
            good,
        ]
    )
    assert [s.log_id for s in sessions] == ["3"]
    assert report.skipped_row_count == 2
    texts = [w.description for w in report.warnings]
    assert any("sleep log 1" in t and "startTime" in t for t in texts)
    assert any("sleep log 2" in t and "end time" in t for t in texts)
    assert all(w.subject == FILE for w in report.warnings)


def test_file_level_skips():
    sessions, report = _parse('[\n{"logId": 1,\n')
    assert sessions is None
    assert report.skipped_files[0][0] == FILE and "line 3" in report.skipped_files[0][1]

    sessions, report = _parse({"sleep": []})
    assert sessions is None and "not an array" in report.skipped_files[0][1]


def test_dst_fall_back_ordering_uses_utc():
    # 01:30 occurs twice on 2024-11-03 in New York; the earlier (EDT) is used.
    log = {
        "logId": 9,
        "startTime": "2024-11-03T00:30:00",
        "endTime": "2024-11-03T03:00:00",
        "levels": {"data": [{"dateTime": "2024-11-03T00:30:00", "level": "light", "seconds": 12600}]},
    }
    sessions, _ = _parse([log])
    (s,) = sessions
    assert s.start_time == _utc(2024, 11, 3, 4, 30)
    assert s.end_time == _utc(2024, 11, 3, 8)  # 03:00 EST
    assert s.stages[0].end_time == s.end_time
