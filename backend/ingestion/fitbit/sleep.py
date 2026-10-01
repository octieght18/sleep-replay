"""Fitbit sleep-log parsing: ``sleep-YYYY-MM-DD.json`` -> candidate SleepSessions.

One candidate SleepSession per entry of the file's JSON array (Requirement
3.1), with Stage_Segments built from ``levels.data`` (3.2) and
``levels.shortData`` Brief_Awakenings (3.3, 3.4) through
``backend.domain.stages.build_stage_segments`` (overlap rule 2.8, clipping
2.9). Level names map through the Stage_Name_Mapping for both ``stages`` and
``classic`` logs (3.5).

Report bookkeeping (all written into the caller's ``Import_Report``):

* File skips (Requirement 5.7): bytes that are not UTF-8 text, invalid JSON
  (reason names the line of the first parse error), or a top-level value
  other than an array. ``parse_sleep_json`` then returns ``None``.
* Entry skips: a non-object entry or a missing/unparseable ``startTime`` or
  ``endTime`` skips the entry, adds 1 to the skipped-row count (5.8), and adds
  a warning naming the file and the logId where present (3.9). An entry with
  ``endTime <= startTime`` is excluded with a warning naming the file (2.7).
* Levels entries with a missing/unparseable ``dateTime`` or a ``seconds``
  value that is missing, non-numeric, non-finite, or <= 0 are skipped, adding
  1 to the skipped-value count each (3.10).
* One warning per distinct unknown level name per file, giving the number of
  ``levels.data`` entries with that name (3.7).
* A session with no valid ``levels.data`` entry (levels absent, data empty,
  or every entry skipped) gets one ``unknown`` segment spanning the session
  plus a warning (3.6).

``accepted_files`` and applied Source_Timezones are left to the caller (the
Fitbit_Importer adapter). DST adjustments are counted in the optional
``DstAdjustmentCounter``; the caller emits its warnings.

Timezones: every timestamp is localized in the sleep Source_Timezone (an
explicit offset wins, Requirement 9.3) and then converted to UTC, so ordering
and durations stay correct across a DST fall-back (same-tzinfo datetimes are
compared by wall time, ignoring ``fold``).

This module imports only the standard library, ``backend.domain``, and its
sibling ``timestamps`` module.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from backend.domain.errors import Import_Report
from backend.domain.session import SleepSession
from backend.domain.stages import (
    Sleep_Stage,
    Stage_Interval,
    Stage_Segment,
    build_stage_segments,
    map_stage_name,
)
from backend.domain.timezones import DstAdjustmentCounter
from backend.ingestion.fitbit.timestamps import TimestampParseError, localize_iso

__all__ = [
    "FITBIT_SOURCE",
    "SleepFileLike",
    "parse_sleep_file",
    "parse_sleep_json",
    "parse_sleep_logs",
]

#: Source identifier stored on every SleepSession this module creates.
FITBIT_SOURCE = "fitbit"

_UNREADABLE_TEXT_REASON = "The file could not be read as UTF-8 text."
_NON_ARRAY_REASON = "The file's top-level JSON value is not an array of sleep logs."


class SleepFileLike(Protocol):
    """Anything with a ``name`` and in-memory ``data`` (e.g. ``packaging.FitbitFile``)."""

    name: str
    data: bytes


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def parse_sleep_file(
    file: SleepFileLike,
    tz: ZoneInfo,
    report: Import_Report,
    counter: DstAdjustmentCounter | None = None,
) -> list[SleepSession] | None:
    """Parse a matched Fitbit sleep file. See :func:`parse_sleep_json`."""
    return parse_sleep_json(file.data, file.name, tz, report, counter)


def parse_sleep_json(
    data: bytes | str,
    file_name: str,
    tz: ZoneInfo,
    report: Import_Report,
    counter: DstAdjustmentCounter | None = None,
) -> list[SleepSession] | None:
    """Decode a sleep file's JSON and parse its sleep logs.

    Returns the candidate SleepSessions (possibly empty), or ``None`` when the
    whole file was skipped; the skip reason is then in ``report.skipped_files``.
    A UTF-8 byte order mark is tolerated.
    """
    if isinstance(data, (bytes, bytearray)):
        try:
            text = bytes(data).decode("utf-8-sig")
        except UnicodeDecodeError:
            report.skip_file(file_name, _UNREADABLE_TEXT_REASON)
            return None
    else:
        text = data.removeprefix("\ufeff")

    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        report.skip_file(
            file_name,
            f"The file is not valid JSON (first parse error on line {exc.lineno}).",
        )
        return None

    if not isinstance(payload, list):
        report.skip_file(file_name, _NON_ARRAY_REASON)
        return None

    return parse_sleep_logs(payload, file_name, tz, report, counter)


def parse_sleep_logs(
    entries: list[Any],
    file_name: str,
    tz: ZoneInfo,
    report: Import_Report,
    counter: DstAdjustmentCounter | None = None,
) -> list[SleepSession]:
    """Turn the decoded JSON array of one sleep file into candidate SleepSessions.

    Sessions are returned in file order. Warnings and counts go to ``report``
    as described in the module docstring.
    """
    # Distinct unknown level name (normalized) -> [display name, entry count].
    unknown_levels: dict[str, list[Any]] = {}
    sessions: list[SleepSession] = []

    for entry in entries:
        session = _parse_entry(entry, file_name, tz, report, counter, unknown_levels)
        if session is not None:
            sessions.append(session)

    for display, count in unknown_levels.values():
        noun = "stage segment" if count == 1 else "stage segments"
        report.add_warning(
            f"{file_name} contains the unrecognized sleep level '{display}'; "
            f"{count} {noun} with this level were recorded as unknown.",
            subject=file_name,
        )
    return sessions


# ---------------------------------------------------------------------------
# Entry parsing
# ---------------------------------------------------------------------------


def _parse_entry(
    entry: Any,
    file_name: str,
    tz: ZoneInfo,
    report: Import_Report,
    counter: DstAdjustmentCounter | None,
    unknown_levels: dict[str, list[Any]],
) -> SleepSession | None:
    if not isinstance(entry, Mapping):
        report.skipped_row_count += 1
        report.add_warning(
            f"A sleep log in {file_name} was skipped because it is not a JSON object "
            "with startTime and endTime.",
            subject=file_name,
        )
        return None

    log_id = _log_id(entry.get("logId"))
    log_ref = f"sleep log {log_id}" if log_id is not None else "a sleep log without a logId"

    start = _to_utc(entry.get("startTime"), tz, file_name, counter)
    end = _to_utc(entry.get("endTime"), tz, file_name, counter)
    if start is None or end is None:
        missing = [
            name
            for name, value in (("startTime", start), ("endTime", end))
            if value is None
        ]
        report.skipped_row_count += 1
        report.add_warning(
            f"In {file_name}, {log_ref} was skipped because its "
            f"{' and '.join(missing)} {'is' if len(missing) == 1 else 'are'} "
            "missing or not a valid timestamp.",
            subject=file_name,
        )
        return None

    if end <= start:
        report.add_warning(
            f"In {file_name}, {log_ref} was skipped because its end time is not "
            "later than its start time.",
            subject=file_name,
        )
        return None

    stages = _build_stages(
        entry.get("levels"), start, end, file_name, log_ref, tz, report, counter, unknown_levels
    )

    is_main = entry.get("isMainSleep")
    return SleepSession(
        start_time=start,
        end_time=end,
        source=FITBIT_SOURCE,
        stages=tuple(stages),
        log_id=log_id,
        is_main_sleep=is_main if isinstance(is_main, bool) else False,
        source_file=file_name,
    )


def _build_stages(
    levels: Any,
    start: datetime,
    end: datetime,
    file_name: str,
    log_ref: str,
    tz: ZoneInfo,
    report: Import_Report,
    counter: DstAdjustmentCounter | None,
    unknown_levels: dict[str, list[Any]],
) -> list[Stage_Segment]:
    data_entries: list[Any] = []
    short_entries: list[Any] = []
    if isinstance(levels, Mapping):
        if isinstance(levels.get("data"), list):
            data_entries = levels["data"]
        if isinstance(levels.get("shortData"), list):
            short_entries = levels["shortData"]

    intervals: list[Stage_Interval] = []
    # Unknown names are tallied only for valid levels entries (skipped ones
    # produce no Stage_Segment).
    for item in data_entries:
        span = _level_span(item, tz, file_name, counter)
        if span is None:
            report.skipped_value_count += 1
            continue
        raw_level = item.get("level")
        stage = map_stage_name(raw_level)
        if stage is Sleep_Stage.unknown:
            key, display = _level_key(raw_level)
            tally = unknown_levels.setdefault(key, [display, 0])
            tally[1] += 1
        intervals.append(Stage_Interval(span[0], span[1], stage))

    has_valid_data = bool(intervals)

    for item in short_entries:
        span = _level_span(item, tz, file_name, counter)
        if span is None:
            report.skipped_value_count += 1
            continue
        # shortData entries are awake Brief_Awakenings whatever their level
        # name says (Requirement 3.3, 3.4).
        intervals.append(
            Stage_Interval(span[0], span[1], Sleep_Stage.awake, is_brief_awakening=True)
        )

    if not has_valid_data:
        report.add_warning(
            f"Stage information is unavailable for {log_ref} in {file_name}; "
            "the whole session is treated as unknown stage.",
            subject=file_name,
        )
        return [Stage_Segment(start, end, Sleep_Stage.unknown)]

    return build_stage_segments(intervals, start, end)


# ---------------------------------------------------------------------------
# Field helpers
# ---------------------------------------------------------------------------


def _to_utc(
    value: Any,
    tz: ZoneInfo,
    file_name: str,
    counter: DstAdjustmentCounter | None,
) -> datetime | None:
    """Localize an ISO 8601 value in ``tz`` and convert to UTC; None if unparseable."""
    try:
        return localize_iso(value, tz, file_name, counter).astimezone(timezone.utc)
    except (TimestampParseError, OverflowError):
        return None


def _seconds(value: Any) -> float | None:
    """A positive, finite seconds value, or None (Requirement 3.10)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        seconds = float(value)
    elif isinstance(value, str):
        try:
            seconds = float(value.strip())
        except ValueError:
            return None
    else:
        return None
    if not math.isfinite(seconds) or seconds <= 0:
        return None
    return seconds


def _level_span(
    item: Any,
    tz: ZoneInfo,
    file_name: str,
    counter: DstAdjustmentCounter | None,
) -> tuple[datetime, datetime] | None:
    """``(start, start + seconds)`` in UTC for a valid levels entry, else None."""
    if not isinstance(item, Mapping):
        return None
    seconds = _seconds(item.get("seconds"))
    if seconds is None:
        return None
    start = _to_utc(item.get("dateTime"), tz, file_name, counter)
    if start is None:
        return None
    try:
        return start, start + timedelta(seconds=seconds)
    except OverflowError:
        return None


def _level_key(raw: Any) -> tuple[str, str]:
    """(normalized key, display name) for an unrecognized level name."""
    if isinstance(raw, str):
        return raw.strip().casefold(), raw.strip()
    if raw is None:
        return "\x00missing", "(missing)"
    return f"\x00{raw!r}", repr(raw)


def _log_id(value: Any) -> str | None:
    """logId as text; None when absent or not a string/integer."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None
