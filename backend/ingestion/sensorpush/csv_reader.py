"""SensorPush_CSV reading and header matching (Requirements 6.2, 6.5, 6.12).

Reading (6.5)
-------------
* Input is the raw file bytes (or already-decoded text). Bytes are decoded as
  UTF-8; a leading UTF-8 byte order mark is removed so it never becomes part
  of the first column header. Files that are not valid UTF-8 fall back to
  Windows-1252 (common for ``°`` in Excel-saved exports).
* The delimiter is detected from the header line: semicolon when the header
  has more semicolons than commas outside double quotes, otherwise comma.
* Blank lines are dropped and never counted as skipped rows. A line is blank
  when it is empty, whitespace-only, or consists only of delimiters with empty
  cells (for example the ``,,,`` rows spreadsheet tools append).
* The first non-blank line is the header row; every later non-blank line is a
  data row, returned as a :class:`CsvRow` with its 1-based physical line
  number.

Header matching (6.2, 6.12)
---------------------------
Each header is lower-cased and checked against the Input Format Assumptions
keywords. Rules are applied in this precedence, and the first rule that
applies decides the column type:

1. **Ignored**: headers containing ``dew`` (dew point) or a vapor pressure
   deficit marker (``vpd``, ``vapor``, ``vapour``, ``deficit``). These are
   checked first because a header like ``Vapor Pressure Deficit (kPa)``
   would otherwise match the pressure keyword.
2. **Temperature**: contains ``temp``.
3. **Relative humidity**: contains ``humidity`` or has ``rh`` as a separate
   token (split on non-alphanumeric characters, so ``RH (%)`` and ``%RH``
   match but ``north`` does not), and does not contain ``absolute``. An
   absolute-humidity header is ignored.
4. **Barometric pressure**: contains ``pressure`` or ``baro``.
5. **Sensor identifier**: contains ``sensor id``, ``sensorid``, ``sensor``, or
   ``device``.
6. **Timestamp**: contains ``timestamp``, ``observed``, ``datetime``, ``date``,
   or ``time``.

Metric and sensor-id rules come before the timestamp rule so the broad
``date``/``time`` keywords cannot claim a measurement column. Headers that
match no rule, and empty headers, are ignored without a warning.

When more than one column resolves to the same type, the leftmost column is
used and the others are ignored with one warning per column type naming the
ignored headers (6.12).

Units are not interpreted here: each :class:`MatchedColumn` carries the raw
header text for ``units.py`` (Requirement 6.3).

This module imports only the standard library and ``backend.domain``.
"""

from __future__ import annotations

import csv
import io
import os
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Final

from backend.domain.errors import Warning_Item

__all__ = [
    "ColumnType",
    "MEASUREMENT_COLUMN_TYPES",
    "EXPECTED_HEADER_KEYWORDS",
    "CsvFormatError",
    "CsvRow",
    "MatchedColumn",
    "HeaderMatch",
    "SensorPushCsv",
    "decode_csv_bytes",
    "detect_delimiter",
    "classify_header",
    "match_headers",
    "parse_csv",
    "read_csv_file",
]

UTF8_BOM: Final = "\ufeff"


class ColumnType(StrEnum):
    """Recognized SensorPush column types. Measurement values equal the
    matching ``domain.telemetry.Metric`` values."""

    TIMESTAMP = "timestamp"
    TEMPERATURE = "temperature"
    HUMIDITY = "humidity"
    PRESSURE = "pressure"
    SENSOR_ID = "sensor_id"


#: Column types that produce TelemetryPoints, in report order.
MEASUREMENT_COLUMN_TYPES: Final[tuple[ColumnType, ...]] = (
    ColumnType.TEMPERATURE,
    ColumnType.HUMIDITY,
    ColumnType.PRESSURE,
)

#: Human-readable keyword descriptions per column type, for the
#: ``NO_TIMESTAMP_COLUMN`` User_Error (Requirement 6.7).
EXPECTED_HEADER_KEYWORDS: Final[Mapping[ColumnType, str]] = {
    ColumnType.TIMESTAMP: "timestamp, observed, date, time, datetime",
    ColumnType.TEMPERATURE: "temp (not dew)",
    ColumnType.HUMIDITY: "humidity, rh (not absolute)",
    ColumnType.PRESSURE: "pressure, baro",
    ColumnType.SENSOR_ID: "sensor id, sensorid, sensor, device",
}

_IGNORED_KEYWORDS: Final = ("dew", "vpd", "vapor", "vapour", "deficit")
_TIMESTAMP_KEYWORDS: Final = ("timestamp", "observed", "datetime", "date", "time")
_SENSOR_KEYWORDS: Final = ("sensor id", "sensorid", "sensor", "device")
_TOKEN_SPLIT: Final = re.compile(r"[^0-9a-z]+")


class CsvFormatError(ValueError):
    """The file could not be parsed as CSV.

    Attributes:
        line_number: 1-based physical line where parsing failed, if known.
    """

    def __init__(self, message: str, line_number: int | None = None) -> None:
        super().__init__(message)
        self.line_number = line_number


@dataclass(frozen=True)
class CsvRow:
    """One non-blank data row.

    Attributes:
        line_number: 1-based physical line number in the file (the last line
            of the record when a quoted cell spans lines).
        cells: Cell texts as read, without surrounding-whitespace stripping.
    """

    line_number: int
    cells: tuple[str, ...]

    def cell(self, index: int) -> str:
        """The cell at ``index`` stripped of surrounding whitespace, or ``""``
        when the row is shorter than the header."""
        if 0 <= index < len(self.cells):
            return self.cells[index].strip()
        return ""


@dataclass(frozen=True)
class MatchedColumn:
    """A header resolved to a column type.

    Attributes:
        column_type: The resolved type.
        index: 0-based column position.
        header: Header text as in the file, with the BOM and surrounding
            whitespace removed. ``units.py`` reads unit tokens from it.
    """

    column_type: ColumnType
    index: int
    header: str


@dataclass(frozen=True)
class HeaderMatch:
    """Result of matching a header row.

    Attributes:
        columns: Selected (leftmost) column per matched type.
        ignored_duplicates: Type -> headers of the ignored duplicate columns.
        warnings: One warning per type that had duplicates (Requirement 6.12).
    """

    columns: Mapping[ColumnType, MatchedColumn] = field(default_factory=dict)
    ignored_duplicates: Mapping[ColumnType, tuple[str, ...]] = field(default_factory=dict)
    warnings: tuple[Warning_Item, ...] = ()

    @property
    def timestamp(self) -> MatchedColumn | None:
        return self.columns.get(ColumnType.TIMESTAMP)

    @property
    def sensor_id(self) -> MatchedColumn | None:
        return self.columns.get(ColumnType.SENSOR_ID)

    @property
    def measurement_columns(self) -> tuple[MatchedColumn, ...]:
        """Matched measurement columns in ``MEASUREMENT_COLUMN_TYPES`` order."""
        return tuple(self.columns[t] for t in MEASUREMENT_COLUMN_TYPES if t in self.columns)

    @property
    def missing_measurements(self) -> tuple[ColumnType, ...]:
        """Measurement types with no matching column (Requirement 6.10)."""
        return tuple(t for t in MEASUREMENT_COLUMN_TYPES if t not in self.columns)


@dataclass(frozen=True)
class SensorPushCsv:
    """A read SensorPush_CSV.

    Attributes:
        file_name: Name used as the warning subject, if given.
        delimiter: ``","`` or ``";"``.
        headers: All header texts (BOM and surrounding whitespace removed).
            Empty when the file has no non-blank line.
        rows: Non-blank data rows in file order.
        match: Header matching result, including duplicate-column warnings.
    """

    file_name: str | None
    delimiter: str
    headers: tuple[str, ...]
    rows: tuple[CsvRow, ...]
    match: HeaderMatch

    @property
    def warnings(self) -> tuple[Warning_Item, ...]:
        return self.match.warnings


# ---------------------------------------------------------------------------
# Decoding and delimiter detection
# ---------------------------------------------------------------------------


def decode_csv_bytes(data: bytes) -> str:
    """Decode file bytes as UTF-8 (BOM removed), falling back to Windows-1252."""
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("cp1252", errors="replace")
    return _strip_bom(text)


def _strip_bom(text: str) -> str:
    return text[1:] if text.startswith(UTF8_BOM) else text


def detect_delimiter(header_line: str) -> str:
    """Return ``";"`` when ``header_line`` has more semicolons than commas
    outside double-quoted sections, otherwise ``","``."""
    commas = semicolons = 0
    in_quotes = False
    for ch in header_line:
        if ch == '"':
            in_quotes = not in_quotes
        elif not in_quotes:
            if ch == ",":
                commas += 1
            elif ch == ";":
                semicolons += 1
    return ";" if semicolons > commas else ","


def _first_non_blank_line(text: str) -> str:
    for line in text.splitlines():
        if line.strip():
            return line
    return ""


# ---------------------------------------------------------------------------
# Header matching
# ---------------------------------------------------------------------------


def classify_header(header: str) -> ColumnType | None:
    """Resolve one header to a column type, or ``None`` when it is ignored.

    Applies the precedence documented in the module docstring.
    """
    text = _strip_bom(header).strip().lower()
    if not text:
        return None
    if any(k in text for k in _IGNORED_KEYWORDS):
        return None
    if "temp" in text:
        return ColumnType.TEMPERATURE
    tokens = _TOKEN_SPLIT.split(text)
    if "humidity" in text or "rh" in tokens:
        return None if "absolute" in text else ColumnType.HUMIDITY
    if "pressure" in text or "baro" in text:
        return ColumnType.PRESSURE
    if any(k in text for k in _SENSOR_KEYWORDS):
        return ColumnType.SENSOR_ID
    if any(k in text for k in _TIMESTAMP_KEYWORDS):
        return ColumnType.TIMESTAMP
    return None


_TYPE_LABELS: Final[Mapping[ColumnType, str]] = {
    ColumnType.TIMESTAMP: "timestamp",
    ColumnType.TEMPERATURE: "temperature",
    ColumnType.HUMIDITY: "relative humidity",
    ColumnType.PRESSURE: "barometric pressure",
    ColumnType.SENSOR_ID: "sensor identifier",
}


def match_headers(headers: Sequence[str], file_name: str | None = None) -> HeaderMatch:
    """Match a header row to column types (Requirements 6.2, 6.12).

    The leftmost column of each type is selected. Every other column of the
    same type is ignored and named in one warning for that type, with
    ``file_name`` as the warning subject.
    """
    columns: dict[ColumnType, MatchedColumn] = {}
    duplicates: dict[ColumnType, list[str]] = {}
    for index, raw in enumerate(headers):
        column_type = classify_header(raw)
        if column_type is None:
            continue
        header = _strip_bom(raw).strip()
        if column_type in columns:
            duplicates.setdefault(column_type, []).append(header)
        else:
            columns[column_type] = MatchedColumn(column_type, index, header)

    warnings: list[Warning_Item] = []
    for column_type in ColumnType:  # stable warning order
        ignored = duplicates.get(column_type)
        if not ignored:
            continue
        label = _TYPE_LABELS[column_type]
        used = columns[column_type].header
        names = ", ".join(f'"{h}"' for h in ignored)
        warnings.append(
            Warning_Item(
                description=(
                    f'More than one {label} column was found. The leftmost column "{used}" was used '
                    f"and these columns were ignored: {names}."
                ),
                subject=file_name,
                recommended_action=(
                    "If an ignored column holds the data you want, remove or rename the other "
                    f"{label} columns and import the file again."
                ),
            )
        )
    return HeaderMatch(
        columns=columns,
        ignored_duplicates={t: tuple(h) for t, h in duplicates.items()},
        warnings=tuple(warnings),
    )


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def _is_blank(cells: Iterable[str]) -> bool:
    return all(not c.strip() for c in cells)


def parse_csv(content: str | bytes, file_name: str | None = None) -> SensorPushCsv:
    """Read SensorPush_CSV content (Requirement 6.5) and match its headers.

    Args:
        content: Raw file bytes or decoded text.
        file_name: Used as the subject of duplicate-column warnings.

    Raises:
        CsvFormatError: the content cannot be parsed as CSV.
    """
    text = decode_csv_bytes(content) if isinstance(content, (bytes, bytearray)) else _strip_bom(content)
    delimiter = detect_delimiter(_first_non_blank_line(text))

    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter, quotechar='"')
    headers: tuple[str, ...] | None = None
    rows: list[CsvRow] = []
    try:
        for cells in reader:
            if _is_blank(cells):
                continue
            if headers is None:
                headers = tuple(_strip_bom(c).strip() for c in cells)
            else:
                rows.append(CsvRow(reader.line_num, tuple(cells)))
    except csv.Error as exc:
        raise CsvFormatError(f"Line {reader.line_num + 1} could not be read as CSV.", reader.line_num + 1) from exc

    headers = headers or ()
    return SensorPushCsv(
        file_name=file_name,
        delimiter=delimiter,
        headers=headers,
        rows=tuple(rows),
        match=match_headers(headers, file_name),
    )


def read_csv_file(path: str | os.PathLike[str], file_name: str | None = None) -> SensorPushCsv:
    """Read a SensorPush_CSV from disk. ``file_name`` defaults to the path's
    base name.

    Raises:
        OSError: the file cannot be read.
        CsvFormatError: the content cannot be parsed as CSV.
    """
    p = Path(path)
    return parse_csv(p.read_bytes(), file_name if file_name is not None else p.name)
