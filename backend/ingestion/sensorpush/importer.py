"""The SensorPush_Importer adapter (Requirements 1.5, 6.1, 6.7-6.11, 6.13, 7.3).

:class:`SensorPushImporter` implements the ``Data_Source_Adapter`` ``load``
operation for SensorPush_CSV files. It has no sleep data, so it defines no
``candidate_sessions`` method and contributes zero candidate SleepSessions
(Requirement 7.2).

Per file
--------
1. ``csv_reader.parse_csv`` reads the file (delimiter, BOM, blank lines) and
   matches the headers (keywords, exclusions, leftmost-duplicate rule).
2. Whole-file errors (the file creates no TelemetryPoints):

   * no non-blank line at all, no data rows, or no row yields a point:
     ``NO_READABLE_ROWS`` listing the supported timestamp formats (6.9);
   * no timestamp column, or none of temperature / humidity / pressure:
     ``NO_TIMESTAMP_COLUMN`` listing the detected headers and expected
     keywords (6.7);
   * unreadable file or malformed CSV: ``SOURCE_LOAD_FAILED``.

3. Each data row's timestamp is parsed with ``timestamps.parse_and_localize``
   in the SensorPush Source_Timezone, counted for the per-file DST warning, and
   converted to UTC. An empty or unsupported timestamp skips the row
   (skipped-row +1, 6.8).
4. Each measurement cell of an accepted row is parsed as a decimal number.
   In semicolon-delimited files a single decimal comma (``21,5``) is accepted.
   Empty, non-numeric, or non-finite cells skip that value only
   (skipped-value +1, 1.5, 6.13).
5. Each measurement column's unit comes from ``units.resolve_unit`` using the
   parseable values of the accepted rows (6.3, 6.4). A column whose unit is
   ``None`` (undeterminable pressure unit, 6.14) creates no points; its
   warning, when set, goes to the Import_Report.
6. One TelemetryPoint per parseable cell (6.1) with ``source`` =
   ``compose_source("sensorpush", <row sensor id>)``, i.e. plain
   ``sensorpush`` when the row's sensor id is empty or there is no sensor
   column (6.11).
7. One warning per missing measurement metric (6.10).

Across files
------------
Files that fail as a whole are listed in ``Import_Report.skipped_files`` with
the error description and the remaining files are imported (16.2). When every
file fails, the first file's User_Error is raised and nothing is imported.

This module imports only the standard library and ``backend.domain``
(Dependency_Rules, Requirement 17.2).
"""

from __future__ import annotations

import math
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Final
from zoneinfo import ZoneInfo

from backend.domain.adapter import LoadResult, Source, TimezoneContext, compose_source, source_paths
from backend.domain.errors import (
    NO_READABLE_ROWS,
    NO_TIMESTAMP_COLUMN,
    SOURCE_LOAD_FAILED,
    Import_Report,
    User_Error,
    Warning_Item,
    merge_import_reports,
)
from backend.domain.telemetry import Metric, TelemetryPoint
from backend.domain.timezones import SENSORPUSH, DstAdjustmentCounter
from backend.ingestion.sensorpush.csv_reader import (
    EXPECTED_HEADER_KEYWORDS,
    ColumnType,
    CsvFormatError,
    SensorPushCsv,
    parse_csv,
)
from backend.ingestion.sensorpush.timestamps import SUPPORTED_FORMATS, InvalidTimestamp, parse_and_localize
from backend.ingestion.sensorpush.units import resolve_unit

__all__ = [
    "SOURCE_IDENTIFIER",
    "SensorPushImporter",
    "parse_number",
    "import_csv",
    "import_file",
]

#: Source_Identifier of the SensorPush_Importer (also the Source_Timezone file type).
SOURCE_IDENTIFIER: Final = SENSORPUSH

_NUMBER_RE: Final = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", re.ASCII)

_METRIC_LABELS: Final[dict[ColumnType, str]] = {
    ColumnType.TIMESTAMP: "timestamp",
    ColumnType.TEMPERATURE: "temperature",
    ColumnType.HUMIDITY: "relative humidity",
    ColumnType.PRESSURE: "barometric pressure",
    ColumnType.SENSOR_ID: "sensor identifier",
}

_DEFAULT_FILE_LABEL: Final = "the SensorPush file"


# ---------------------------------------------------------------------------
# Value parsing
# ---------------------------------------------------------------------------


def parse_number(text: str, delimiter: str = ",") -> float | None:
    """Parse one measurement cell as a finite decimal number, or return ``None``.

    Accepts optional sign, digits with an optional ``.`` fraction, and an
    optional exponent (``NaN``/``inf`` words, digit separators, and trailing
    units are rejected). In semicolon-delimited files (``delimiter == ";"``) a
    value with exactly one comma and no dot is read with the comma as the
    decimal separator (``21,5`` -> 21.5). Values that overflow to infinity are
    rejected (Requirement 1.5).
    """
    if not isinstance(text, str):
        return None
    s = text.strip()
    if not s:
        return None
    if delimiter == ";" and s.count(",") == 1 and "." not in s:
        s = s.replace(",", ".")
    if not _NUMBER_RE.fullmatch(s):
        return None
    value = float(s)
    return value if math.isfinite(value) else None


# ---------------------------------------------------------------------------
# User_Errors
# ---------------------------------------------------------------------------


def _quoted_list(items: tuple[str, ...] | list[str]) -> str:
    return ", ".join(f'"{h}"' for h in items) if items else "(none)"


def _expected_keywords_text() -> str:
    return "; ".join(
        f"{_METRIC_LABELS[t]}: {EXPECTED_HEADER_KEYWORDS[t]}" for t in ColumnType if t in EXPECTED_HEADER_KEYWORDS
    )


def _no_timestamp_column(parsed: SensorPushCsv, label: str) -> User_Error:
    match = parsed.match
    if match.timestamp is None and not match.measurement_columns:
        problem = "no recognizable timestamp column and no temperature, relative humidity, or barometric pressure column"
    elif match.timestamp is None:
        problem = "no recognizable timestamp column"
    else:
        problem = "no temperature, relative humidity, or barometric pressure column"
    detected = _quoted_list(list(parsed.headers))
    expected = _expected_keywords_text()
    return User_Error(
        code=NO_TIMESTAMP_COLUMN,
        description=(
            f"{label} has {problem}, so nothing was imported from it. "
            f"Detected column headers: {detected}. Expected header keywords (case-insensitive): {expected}."
        ),
        action=(
            "Check that the file is a SensorPush CSV export whose header row names a timestamp column and at "
            "least one temperature, humidity, or pressure column, then import the file again."
        ),
        file_name=parsed.file_name,
        details={"detected_headers": detected, "expected_keywords": expected},
    )


def _no_readable_rows(label: str, file_name: str | None, has_rows: bool) -> User_Error:
    reason = (
        "no data row has a timestamp in a supported format and a numeric measurement value"
        if has_rows
        else "the file contains no data rows"
    )
    formats = "; ".join(SUPPORTED_FORMATS)
    return User_Error(
        code=NO_READABLE_ROWS,
        description=(
            f"No readable rows were found in {label}: {reason}, so nothing was imported from it. "
            f"Supported timestamp formats: {formats}."
        ),
        action=(
            "Export the data from the SensorPush app again and check that the timestamp column uses one of the "
            "supported formats and the measurement columns contain numbers, then import the file again."
        ),
        file_name=file_name,
        details={"supported_formats": formats},
    )


def _load_failed(label: str, file_name: str | None, reason: str) -> User_Error:
    return User_Error(
        code=SOURCE_LOAD_FAILED,
        description=f"{label} could not be read: {reason} Nothing was imported from it.",
        action="Check that the file is a SensorPush CSV export that is not open in another program, then import it again.",
        file_name=file_name,
    )


def _missing_metric_warning(column_type: ColumnType, label: str) -> Warning_Item:
    metric = column_type.value
    return Warning_Item(
        description=(
            f"{metric.capitalize()} data is unavailable: {label} has no {_METRIC_LABELS[column_type]} column."
        ),
        subject=metric,
        recommended_action=(
            f"If you expected {metric} data, check that the file has a column whose header contains "
            f"{EXPECTED_HEADER_KEYWORDS[column_type]}, then import it again."
        ),
    )


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------


def import_csv(parsed: SensorPushCsv, source_tz: ZoneInfo) -> LoadResult:
    """Turn one read SensorPush_CSV into TelemetryPoints and an Import_Report.

    Timestamps without an offset are interpreted in ``source_tz``; every
    TelemetryPoint timestamp is in UTC. The report lists the file as accepted
    and records skipped rows/values, per-metric counts and coverage, the
    applied Source_Timezone, and warnings (duplicate columns, missing metrics,
    unit inference, DST adjustments).

    Raises:
        User_Error: ``NO_TIMESTAMP_COLUMN`` (6.7) or ``NO_READABLE_ROWS`` (6.9);
            no TelemetryPoints are created from the file.
    """
    file_name = parsed.file_name
    label = file_name or _DEFAULT_FILE_LABEL
    if not parsed.headers:
        raise _no_readable_rows(label, file_name, has_rows=False)
    match = parsed.match
    if match.timestamp is None or not match.measurement_columns:
        raise _no_timestamp_column(parsed, label)
    if not parsed.rows:
        raise _no_readable_rows(label, file_name, has_rows=False)

    report = Import_Report()
    dst = DstAdjustmentCounter()
    ts_index = match.timestamp.index
    sensor_index = match.sensor_id.index if match.sensor_id is not None else None
    columns = match.measurement_columns

    # Pass 1: timestamps (row-level skip) and cell values.
    rows: list[tuple[datetime, str, list[float | None]]] = []
    for row in parsed.rows:
        try:
            localized = parse_and_localize(row.cell(ts_index), source_tz)
        except InvalidTimestamp:
            report.skipped_row_count += 1
            continue
        # Convert to UTC: same-ZoneInfo datetimes compare by wall time across DST fall-back.
        timestamp = dst.record(label, localized).astimezone(timezone.utc)
        source = compose_source(SOURCE_IDENTIFIER, row.cell(sensor_index) if sensor_index is not None else None)
        rows.append((timestamp, source, [parse_number(row.cell(c.index), parsed.delimiter) for c in columns]))

    # Units per column, from the parseable values of the accepted rows.
    units: list[str | None] = []
    unit_warnings: list[Warning_Item] = []
    for pos, column in enumerate(columns):
        values = [v[pos] for _, _, v in rows if v[pos] is not None]
        resolution = resolve_unit(Metric(column.column_type.value), column.header, values, file_name)
        units.append(resolution.unit)
        if resolution.warning is not None:
            unit_warnings.append(resolution.warning)

    # Pass 2: one TelemetryPoint per parseable cell, row by row.
    telemetry: list[TelemetryPoint] = []
    counts = [0] * len(columns)
    first: list[datetime | None] = [None] * len(columns)
    last: list[datetime | None] = [None] * len(columns)
    for timestamp, source, values in rows:
        for pos, value in enumerate(values):
            if value is None:
                report.skipped_value_count += 1
                continue
            unit = units[pos]
            if unit is None:  # column dropped (undeterminable unit); warned above
                continue
            metric = Metric(columns[pos].column_type.value)
            telemetry.append(TelemetryPoint(timestamp, source, metric, value, unit))
            counts[pos] += 1
            if first[pos] is None or timestamp < first[pos]:
                first[pos] = timestamp
            if last[pos] is None or timestamp > last[pos]:
                last[pos] = timestamp

    if not telemetry:
        raise _no_readable_rows(label, file_name, has_rows=True)

    for pos, column in enumerate(columns):
        if counts[pos]:
            report.record_metric(column.column_type.value, counts[pos], (first[pos], last[pos]))  # type: ignore[arg-type]

    if file_name:
        report.accepted_files.append(file_name)
    report.applied_source_timezones[SENSORPUSH] = source_tz.key or str(source_tz)
    report.warnings.extend(parsed.warnings)
    report.warnings.extend(_missing_metric_warning(t, label) for t in match.missing_measurements)
    report.warnings.extend(unit_warnings)
    report.warnings.extend(dst.warnings())
    return LoadResult(telemetry=telemetry, report=report)


def import_file(path: str | os.PathLike[str], source_tz: ZoneInfo) -> LoadResult:
    """Read and import one SensorPush_CSV file (see :func:`import_csv`).

    Raises:
        User_Error: ``SOURCE_LOAD_FAILED`` when the file can't be read or isn't
            valid CSV, otherwise as for :func:`import_csv`.
    """
    p = Path(path)
    name = p.name
    try:
        data = p.read_bytes()
    except OSError:
        raise _load_failed(name, name, "the file could not be opened.") from None
    try:
        parsed = parse_csv(data, name)
    except CsvFormatError as exc:
        raise _load_failed(name, name, f"it is not a valid CSV file ({exc}).") from None
    return import_csv(parsed, source_tz)


class SensorPushImporter:
    """``Data_Source_Adapter`` for SensorPush_CSV files (``load`` only).

    ``source`` is one or more local CSV file paths. The SensorPush
    Source_Timezone comes from ``tz_context.source_timezone("sensorpush")``
    (the user's override, else the Display_Timezone) and is recorded in the
    Import_Report.
    """

    source_identifier: str = SOURCE_IDENTIFIER

    def load(self, source: Source, tz_context: TimezoneContext) -> LoadResult:
        """Import every file in ``source``.

        Files that fail as a whole are listed in ``report.skipped_files`` and
        the others are imported (Requirement 16.2).

        Raises:
            User_Error: ``INVALID_TIMEZONE`` for an invalid SensorPush override;
                ``SOURCE_LOAD_FAILED`` when ``source`` names no file; otherwise
                the first file's error when no file could be imported.
        """
        source_tz = tz_context.source_timezone(SENSORPUSH)
        paths = source_paths(source)
        if not paths:
            raise User_Error(
                code=SOURCE_LOAD_FAILED,
                description="No SensorPush file was provided, so nothing was imported.",
                action="Select a SensorPush CSV export file and import it again.",
            )

        results: list[LoadResult] = []
        errors: list[User_Error] = []
        for path in paths:
            try:
                results.append(import_file(path, source_tz))
            except User_Error as err:
                errors.append(err)
        if not results:
            raise errors[0]

        report = merge_import_reports(r.report for r in results)
        for err in errors:
            report.skip_file(err.file_name or _DEFAULT_FILE_LABEL, err.description)
        report.applied_source_timezones[SENSORPUSH] = source_tz.key or str(source_tz)
        telemetry = [point for r in results for point in r.telemetry]
        return LoadResult(telemetry=telemetry, report=report)
