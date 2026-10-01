"""Fitbit HRV parsing: HRV details and the daily HRV summary.

Two Fitbit CSV file types carry heart rate variability:

* ``Heart Rate Variability Details - YYYY-MM-DD.csv``: one row per sample
  (typically every 5 minutes). Each data row gives one ``hrv_rmssd``
  TelemetryPoint in ms, with the timestamp from the ``timestamp`` column
  (ISO 8601) and the value from the ``rmssd`` column. All other columns are
  ignored (Requirement 4.2).
* ``Daily Heart Rate Variability Summary - YYYY-MM(-DD).csv``: one row per
  night. Each data row gives one :class:`~backend.domain.adapter.SessionHrvRow`
  with the calendar date of the ``timestamp`` column as written and the
  ``rmssd`` value. :func:`session_hrv_for` turns these rows into a Session_HRV
  for a SleepSession (4.3); the pipeline attaches it only when the session has
  no in-session hrv_rmssd TelemetryPoint.

CSV handling: bytes are decoded as UTF-8 with an optional byte order mark. The
first non-blank row is the header; ``timestamp`` and ``rmssd`` are matched
case-insensitively after trimming whitespace (the first matching column wins).
Blank lines are ignored without being counted.

Offset-free details timestamps are interpreted in the HRV details
Source_Timezone (Display_Timezone by default; the caller passes the resolved
``ZoneInfo``), DST-adjusted and counted per file through
``DstAdjustmentCounter``, then converted to UTC. An explicit offset wins.

Skip rules (per file; totals also added to the Import_Report when one is given):

* skipped-row count: the ``timestamp`` cell is missing or can't be parsed as an
  ISO 8601 date-time (4.7, 5.8). A row skip wins over a value skip.
* skipped-value count: the ``rmssd`` cell is missing, empty, not a decimal
  number, non-finite, or outside the range gate 0 < rmssd <= 500 ms (1.5, 4.8).
* file skip (``Import_Report.skipped_files``): the bytes are not UTF-8, the CSV
  can't be parsed (reason gives the line of the first parse error), or the
  header lacks a ``timestamp`` or ``rmssd`` column (reason names the missing
  column) (5.7). Nothing comes from a skipped file.

Values are stored as ``float`` in ms, without conversion (1.4). Files are not
added to ``Import_Report.accepted_files`` and per-metric counts are not
recorded here; the Fitbit_Importer does that.

This module imports only the standard library, ``backend.domain``, and sibling
modules of this sub-package.
"""

from __future__ import annotations

import csv
import io
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import timezone
from zoneinfo import ZoneInfo

from backend.domain.adapter import SessionHrvRow, compose_source
from backend.domain.errors import Import_Report
from backend.domain.session import SleepSession
from backend.domain.telemetry import Metric, TelemetryPoint
from backend.domain.timezones import DstAdjustmentCounter
from backend.domain.units import MS

from .intraday import FITBIT_SOURCE_IDENTIFIER, IntradayParseResult, parse_number
from .timestamps import TimestampParseError, localize_iso, parse_iso_datetime

__all__ = [
    "RMSSD_RANGE",
    "TIMESTAMP_COLUMN",
    "RMSSD_COLUMN",
    "HrvDetailsParseResult",
    "HrvSummaryParseResult",
    "CsvFileError",
    "read_hrv_csv",
    "in_rmssd_range",
    "parse_hrv_details",
    "parse_hrv_summary",
    "session_hrv_for",
]

_SOURCE = compose_source(FITBIT_SOURCE_IDENTIFIER)
_UTC = timezone.utc

#: Accepted rmssd range in ms: lower bound exclusive, upper bound inclusive (4.8).
RMSSD_RANGE: tuple[float, float] = (0.0, 500.0)

#: Required header columns (matched case-insensitively after trimming).
TIMESTAMP_COLUMN = "timestamp"
RMSSD_COLUMN = "rmssd"

#: Parse result for an HRV details file: ``metric`` is ``Metric.hrv_rmssd``.
HrvDetailsParseResult = IntradayParseResult


@dataclass
class HrvSummaryParseResult:
    """What parsing one daily HRV summary file produced.

    Attributes:
        file_name: The file name (or archive member path) as reported.
        rows: Dated rmssd rows in file order.
        skipped_value_count: rmssd values skipped as empty, non-numeric,
            non-finite, or out of range.
        skipped_row_count: Rows skipped for a missing or unparseable timestamp.
        file_skip_reason: Why the whole file was skipped, or ``None``.
    """

    file_name: str
    rows: list[SessionHrvRow] = field(default_factory=list)
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
# CSV reading
# ---------------------------------------------------------------------------


class CsvFileError(ValueError):
    """A Fitbit HRV CSV can't be used; ``reason`` is the plain-language skip reason."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _is_blank(row: Sequence[str]) -> bool:
    return all(not cell.strip() for cell in row)


def _missing_columns_reason(missing: list[str]) -> str:
    names = " and ".join(f"'{name}'" for name in missing)
    noun = "column" if len(missing) == 1 else "columns"
    return f"The file has no {names} {noun} in its header row, so it was not imported."


def read_hrv_csv(data: bytes | str) -> tuple[int, int, list[list[str]]]:
    """Decode an HRV CSV and locate its ``timestamp`` and ``rmssd`` columns.

    Accepts bytes (UTF-8, optional byte order mark) or text.

    Returns:
        ``(timestamp_index, rmssd_index, data_rows)`` where ``data_rows`` are
        the non-blank rows after the header, in file order.

    Raises:
        CsvFileError: not UTF-8, not parseable as CSV (reason gives the line of
            the first parse error), or the header lacks ``timestamp`` or
            ``rmssd`` (reason names the missing column).
    """
    if isinstance(data, (bytes, bytearray)):
        try:
            text = bytes(data).decode("utf-8-sig")
        except UnicodeDecodeError:
            raise CsvFileError("The file is not UTF-8 text, so it could not be parsed as CSV.") from None
    else:
        text = data.removeprefix("\ufeff")

    reader = csv.reader(io.StringIO(text, newline=""), strict=True)
    rows: list[list[str]] = []
    try:
        for row in reader:
            if row and not _is_blank(row):
                rows.append(row)
    except csv.Error:
        raise CsvFileError(
            f"The file could not be parsed as CSV (first error on line {reader.line_num})."
        ) from None

    header = [cell.strip().lower() for cell in rows[0]] if rows else []
    indices: dict[str, int] = {}
    for name in (TIMESTAMP_COLUMN, RMSSD_COLUMN):
        if name in header:
            indices[name] = header.index(name)
    missing = [name for name in (TIMESTAMP_COLUMN, RMSSD_COLUMN) if name not in indices]
    if missing:
        raise CsvFileError(_missing_columns_reason(missing))
    return indices[TIMESTAMP_COLUMN], indices[RMSSD_COLUMN], rows[1:]


def _cell(row: Sequence[str], index: int) -> str | None:
    return row[index] if index < len(row) else None


def in_rmssd_range(value: float) -> bool:
    """True iff ``0 < value <= 500`` (Requirement 4.8)."""
    low, high = RMSSD_RANGE
    return low < value <= high


def _rmssd_value(raw: str | None) -> float | None:
    value = parse_number(raw)
    return value if value is not None and in_rmssd_range(value) else None


def _timestamp_parses(raw: str | None) -> bool:
    try:
        parse_iso_datetime(raw)
    except TimestampParseError:
        return False
    return True


# ---------------------------------------------------------------------------
# Public parsers
# ---------------------------------------------------------------------------


def parse_hrv_details(
    data: bytes | str,
    file_name: str,
    tz: ZoneInfo,
    counter: DstAdjustmentCounter | None = None,
    report: Import_Report | None = None,
) -> HrvDetailsParseResult:
    """Parse a ``Heart Rate Variability Details - YYYY-MM-DD.csv`` file.

    Produces one ``hrv_rmssd`` TelemetryPoint in ms per valid data row, with a
    UTC timestamp.

    Args:
        data: Raw file bytes or text.
        file_name: Name used in the report and for DST counting.
        tz: The resolved Source_Timezone for ``FITBIT_HRV_DETAILS``.
        counter: Optional DST adjustment counter (one warning per file later).
        report: When given, skip counts and any file skip are added to it.
    """
    result = HrvDetailsParseResult(file_name, Metric.hrv_rmssd)
    try:
        ts_idx, rmssd_idx, rows = read_hrv_csv(data)
    except CsvFileError as exc:
        result.file_skip_reason = exc.reason
        rows = []
        ts_idx = rmssd_idx = 0

    append = result.points.append
    for row in rows:
        ts_raw = _cell(row, ts_idx)
        value = _rmssd_value(_cell(row, rmssd_idx))
        if value is None:
            if _timestamp_parses(ts_raw):
                result.skipped_value_count += 1
            else:
                result.skipped_row_count += 1
            continue
        try:
            timestamp = localize_iso(ts_raw, tz, file_name, counter).astimezone(_UTC)
        except TimestampParseError:
            result.skipped_row_count += 1
            continue
        append(TelemetryPoint(timestamp, _SOURCE, Metric.hrv_rmssd, value, MS))

    if report is not None:
        result.apply_to(report)
    return result


def parse_hrv_summary(
    data: bytes | str,
    file_name: str,
    report: Import_Report | None = None,
) -> HrvSummaryParseResult:
    """Parse a ``Daily Heart Rate Variability Summary - YYYY-MM(-DD).csv`` file.

    Produces one :class:`SessionHrvRow` per valid data row. The row date is the
    calendar date of the ``timestamp`` cell as written (``2024-01-02`` and
    ``2024-01-02T00:00:00`` both give 2 January 2024); no timezone conversion
    is applied, so the Source_Timezone does not change it.

    Args:
        data: Raw file bytes or text.
        file_name: Name used in the report and recorded on each row.
        report: When given, skip counts and any file skip are added to it.
    """
    result = HrvSummaryParseResult(file_name)
    try:
        ts_idx, rmssd_idx, rows = read_hrv_csv(data)
    except CsvFileError as exc:
        result.file_skip_reason = exc.reason
        rows = []
        ts_idx = rmssd_idx = 0

    append = result.rows.append
    for row in rows:
        try:
            day = parse_iso_datetime(_cell(row, ts_idx)).date()
        except TimestampParseError:
            result.skipped_row_count += 1
            continue
        value = _rmssd_value(_cell(row, rmssd_idx))
        if value is None:
            result.skipped_value_count += 1
            continue
        append(SessionHrvRow(day, value, file_name))

    if report is not None:
        result.apply_to(report)
    return result


# ---------------------------------------------------------------------------
# Session_HRV
# ---------------------------------------------------------------------------


def session_hrv_for(
    session: SleepSession,
    rows: Iterable[SessionHrvRow],
    display_tz: ZoneInfo,
) -> float | None:
    """Session_HRV for ``session``: the mean rmssd of rows dated on the calendar
    date of ``session.end_time`` in ``display_tz`` (Requirement 4.3).

    Returns the single row's value when one row matches, the mean when several
    match, and ``None`` when none does. Whether the session already has
    in-session hrv_rmssd TelemetryPoints is the caller's check.
    """
    end_date = session.end_time.astimezone(display_tz).date()
    values = [row.rmssd for row in rows if row.date == end_date]
    if not values:
        return None
    return math.fsum(values) / len(values)
