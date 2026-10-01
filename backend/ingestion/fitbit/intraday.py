"""Fitbit intraday heart rate and steps parsing.

Two Fitbit file types hold intraday samples as a JSON array of entries with a
``dateTime`` in ``MM/DD/YY HH:MM:SS`` (two-digit years read as 2000-2099):

* ``heart_rate-YYYY-MM-DD.json``: ``{"dateTime": ..., "value": {"bpm": 62, ...}}``
  gives one ``heart_rate`` TelemetryPoint in bpm per entry (Requirement 4.1).
* ``steps-YYYY-MM-DD.json``: ``{"dateTime": ..., "value": "12"}`` (string or
  number) gives one ``steps`` TelemetryPoint in steps/min per entry (4.4).

Offset-free ``dateTime`` values are interpreted in the file type's
Source_Timezone (UTC by default; the caller passes the resolved ``ZoneInfo``),
DST-adjusted and counted per file through ``DstAdjustmentCounter``, then
converted to UTC. Converting matters: datetimes sharing one ``ZoneInfo`` compare
by wall-clock time, so two instants across a DST fall-back would otherwise sort
and compare wrongly.

Skip rules (per file; totals also added to the Import_Report when one is given):

* skipped-row count: the entry is not an object, lacks a required field
  (heart rate: ``dateTime``, ``value.bpm``; steps: ``dateTime``, ``value``), or
  its ``dateTime`` can't be parsed (4.7, 5.8). A row skip wins over a value skip.
* skipped-value count: the value is empty (``null`` or blank string), not a
  decimal number, non-finite (NaN/Infinity), or outside the range gate --
  heart rate 20-250 bpm, steps 0-300 steps/min, both inclusive (1.5, 4.8).
* file skip (``Import_Report.skipped_files``): the bytes are not valid UTF-8
  JSON (reason names the line of the first parse error) or the top-level value
  is not an array (5.7). No points come from a skipped file.

Values are stored as ``float`` in the source unit, without conversion (1.4).
Files are not added to ``Import_Report.accepted_files`` and per-metric counts
are not recorded here; the Fitbit_Importer does that.

This module imports only the standard library and ``backend.domain``.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timezone
from typing import Any
from zoneinfo import ZoneInfo

from backend.domain.adapter import compose_source
from backend.domain.errors import Import_Report
from backend.domain.telemetry import Metric, TelemetryPoint
from backend.domain.timezones import FITBIT_HEART_RATE, FITBIT_STEPS, DstAdjustmentCounter
from backend.domain.units import BPM, STEPS_PER_MIN

from .timestamps import TimestampParseError, localize_mdy, parse_mdy_datetime

__all__ = [
    "FITBIT_SOURCE_IDENTIFIER",
    "HEART_RATE_RANGE",
    "STEPS_RANGE",
    "IntradayParseResult",
    "JsonFileError",
    "load_json_array",
    "parse_number",
    "parse_heart_rate",
    "parse_steps",
    "parse_intraday",
]

#: Source_Identifier of the Fitbit_Importer; the ``source`` of every point here.
FITBIT_SOURCE_IDENTIFIER = "fitbit"
_SOURCE = compose_source(FITBIT_SOURCE_IDENTIFIER)

#: Inclusive accepted heart_rate range in bpm (Requirement 4.8).
HEART_RATE_RANGE: tuple[float, float] = (20.0, 250.0)
#: Inclusive accepted steps range in steps/min (Requirement 4.8).
STEPS_RANGE: tuple[float, float] = (0.0, 300.0)

_UTC = timezone.utc

# A plain decimal number, optionally signed, with optional exponent. Stricter
# than ``float()``, which also accepts "nan", "inf", and "1_000".
_DECIMAL_RE = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")

# Sentinel for a required field that is absent.
_MISSING = object()


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


@dataclass
class IntradayParseResult:
    """What parsing one intraday file produced.

    Attributes:
        file_name: The file name (or archive member path) as reported.
        metric: ``Metric.heart_rate`` or ``Metric.steps``.
        points: TelemetryPoints in file order, timestamps in UTC.
        skipped_value_count: Values skipped as empty, non-numeric, non-finite,
            or out of range.
        skipped_row_count: Entries skipped for a missing field or bad dateTime.
        file_skip_reason: Why the whole file was skipped, or ``None``.
    """

    file_name: str
    metric: Metric
    points: list[TelemetryPoint] = field(default_factory=list)
    skipped_value_count: int = 0
    skipped_row_count: int = 0
    file_skip_reason: str | None = None

    @property
    def skipped(self) -> bool:
        """True when the whole file was skipped."""
        return self.file_skip_reason is not None

    def apply_to(self, report: Import_Report) -> None:
        """Add this file's skip counts, or its file skip, to ``report``."""
        if self.file_skip_reason is not None:
            report.skip_file(self.file_name, self.file_skip_reason)
        report.skipped_value_count += self.skipped_value_count
        report.skipped_row_count += self.skipped_row_count


# ---------------------------------------------------------------------------
# JSON and number helpers
# ---------------------------------------------------------------------------


class JsonFileError(ValueError):
    """A Fitbit JSON file can't be used; ``reason`` is the plain-language skip reason."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def load_json_array(data: bytes | str) -> list[Any]:
    """Decode a Fitbit JSON file whose top-level value must be an array.

    Accepts bytes (UTF-8, optional byte order mark) or text.

    Raises:
        JsonFileError: not UTF-8, not valid JSON (reason gives the line of the
            first parse error), or a top-level value other than an array.
    """
    if isinstance(data, (bytes, bytearray)):
        try:
            text = bytes(data).decode("utf-8-sig")
        except UnicodeDecodeError:
            raise JsonFileError("The file is not UTF-8 text, so it could not be parsed as JSON.") from None
    else:
        text = data.removeprefix("\ufeff")
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise JsonFileError(f"The file could not be parsed as JSON (first error on line {exc.lineno}).") from None
    except RecursionError:
        raise JsonFileError("The file could not be parsed as JSON (nesting is too deep).") from None
    if not isinstance(value, list):
        raise JsonFileError("The file's top-level JSON value is not an array, so it was not imported.")
    return value


def parse_number(raw: object) -> float | None:
    """Return ``raw`` as a finite float, or ``None`` when it is not a usable value.

    Numbers and numeric strings (``"12"``, ``" 7.5 "``, ``"1e2"``) are accepted.
    ``None``, blank strings, booleans, other types, non-decimal strings, and
    NaN/Infinity (including overflowing values) give ``None`` (Requirement 1.5).
    """
    if isinstance(raw, float):
        value = raw
    elif isinstance(raw, int) and not isinstance(raw, bool):
        try:
            value = float(raw)
        except OverflowError:
            return None
    elif isinstance(raw, str):
        text = raw.strip()
        if _DECIMAL_RE.fullmatch(text) is None:
            return None
        value = float(text)
    else:
        return None
    return value if math.isfinite(value) else None


# ---------------------------------------------------------------------------
# Entry parsing
# ---------------------------------------------------------------------------


def _heart_rate_value(entry: dict[str, Any]) -> object:
    value = entry.get("value", _MISSING)
    if not isinstance(value, dict):
        return _MISSING
    return value.get("bpm", _MISSING)


def _steps_value(entry: dict[str, Any]) -> object:
    return entry.get("value", _MISSING)


def _parse_entries(
    entries: list[Any],
    result: IntradayParseResult,
    get_value: Callable[[dict[str, Any]], object],
    unit: str,
    value_range: tuple[float, float],
    tz: ZoneInfo,
    counter: DstAdjustmentCounter | None,
) -> None:
    file_name = result.file_name
    metric = result.metric
    low, high = value_range
    points = result.points
    append = points.append
    skipped_values = 0
    skipped_rows = 0

    for entry in entries:
        if not isinstance(entry, dict):
            skipped_rows += 1
            continue
        date_time = entry.get("dateTime", _MISSING)
        raw = get_value(entry)
        if date_time is _MISSING or raw is _MISSING:
            skipped_rows += 1
            continue

        value = parse_number(raw)
        if value is None or not (low <= value <= high):
            # A bad dateTime makes this a skipped row rather than a skipped value.
            try:
                parse_mdy_datetime(date_time)
            except TimestampParseError:
                skipped_rows += 1
            else:
                skipped_values += 1
            continue

        try:
            timestamp = localize_mdy(date_time, tz, file_name, counter).astimezone(_UTC)
        except TimestampParseError:
            skipped_rows += 1
            continue
        append(TelemetryPoint(timestamp, _SOURCE, metric, value, unit))

    result.skipped_value_count += skipped_values
    result.skipped_row_count += skipped_rows


def _parse_file(
    data: bytes | str | list[Any],
    file_name: str,
    metric: Metric,
    get_value: Callable[[dict[str, Any]], object],
    unit: str,
    value_range: tuple[float, float],
    tz: ZoneInfo,
    counter: DstAdjustmentCounter | None,
    report: Import_Report | None,
) -> IntradayParseResult:
    result = IntradayParseResult(file_name, metric)
    if isinstance(data, list):
        entries = data
    else:
        try:
            entries = load_json_array(data)
        except JsonFileError as exc:
            result.file_skip_reason = exc.reason
            entries = None
    if entries is not None:
        _parse_entries(entries, result, get_value, unit, value_range, tz, counter)
    if report is not None:
        result.apply_to(report)
    return result


# ---------------------------------------------------------------------------
# Public parsers
# ---------------------------------------------------------------------------


def parse_heart_rate(
    data: bytes | str | list[Any],
    file_name: str,
    tz: ZoneInfo,
    counter: DstAdjustmentCounter | None = None,
    report: Import_Report | None = None,
) -> IntradayParseResult:
    """Parse a ``heart_rate-YYYY-MM-DD.json`` file into heart_rate points in bpm.

    Args:
        data: Raw file bytes/text, or an already-decoded JSON array.
        file_name: Name used in the report and for DST counting.
        tz: The resolved Source_Timezone for ``FITBIT_HEART_RATE``.
        counter: Optional DST adjustment counter (one warning per file later).
        report: When given, skip counts and any file skip are added to it.
    """
    return _parse_file(
        data, file_name, Metric.heart_rate, _heart_rate_value, BPM, HEART_RATE_RANGE, tz, counter, report
    )


def parse_steps(
    data: bytes | str | list[Any],
    file_name: str,
    tz: ZoneInfo,
    counter: DstAdjustmentCounter | None = None,
    report: Import_Report | None = None,
) -> IntradayParseResult:
    """Parse a ``steps-YYYY-MM-DD.json`` file into steps points in steps/min.

    ``value`` may be a number or a numeric string. Arguments as for
    :func:`parse_heart_rate`, with ``tz`` resolved for ``FITBIT_STEPS``.
    """
    return _parse_file(data, file_name, Metric.steps, _steps_value, STEPS_PER_MIN, STEPS_RANGE, tz, counter, report)


_PARSERS: dict[str, Callable[..., IntradayParseResult]] = {
    FITBIT_HEART_RATE: parse_heart_rate,
    FITBIT_STEPS: parse_steps,
}


def parse_intraday(
    file_type: str,
    data: bytes | str | list[Any],
    file_name: str,
    tz: ZoneInfo,
    counter: DstAdjustmentCounter | None = None,
    report: Import_Report | None = None,
) -> IntradayParseResult:
    """Dispatch on ``file_type`` (``FITBIT_HEART_RATE`` or ``FITBIT_STEPS``).

    Raises:
        ValueError: ``file_type`` is not an intraday file type.
    """
    parser = _PARSERS.get(file_type)
    if parser is None:
        raise ValueError(f"not a Fitbit intraday file type: {file_type!r}")
    return parser(data, file_name, tz, counter, report)
