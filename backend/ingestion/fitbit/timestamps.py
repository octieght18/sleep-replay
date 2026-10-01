"""Fitbit timestamp parsing and localization.

Two timestamp formats appear in a Fitbit_Export:

* ``MM/DD/YY HH:MM:SS`` in intraday heart rate and steps files (``dateTime``).
  Two-digit years are read as 2000-2099 (Requirement 4.1, 4.4).
* ISO 8601 date-times in sleep logs (``startTime``, ``endTime``,
  ``levels.*.dateTime``, e.g. ``2024-01-01T23:05:30.000``) and HRV CSVs
  (``timestamp``), with or without an explicit offset (Requirement 4.2).

Offset-free timestamps are interpreted in the file type's Source_Timezone and
DST-adjusted through ``backend.domain.timezones.localize``; a timestamp with an
explicit offset (``±HH:MM`` or ``Z``) keeps it (Requirement 9.1, 9.3, 9.4).
Pass a ``DstAdjustmentCounter`` to count adjusted timestamps per file so the
Importer can emit one warning per affected file.

Failure signalling: every parse function raises ``TimestampParseError`` (a
``ValueError`` subclass) for empty, non-string, malformed, or out-of-range
input. Callers catch it and count the entry/row as skipped (Requirement 4.7,
5.8). Nothing here returns ``None``.

This module imports only the standard library and ``backend.domain``.
"""

from __future__ import annotations

import re
from datetime import datetime
from zoneinfo import ZoneInfo

from backend.domain.timezones import DstAdjustmentCounter, localize

__all__ = [
    "TimestampParseError",
    "parse_mdy_datetime",
    "parse_iso_datetime",
    "localize_mdy",
    "localize_iso",
]


class TimestampParseError(ValueError):
    """A Fitbit timestamp is empty, not a string, or matches no supported format."""

    def __init__(self, value: object, expected: str) -> None:
        self.value = value
        self.expected = expected
        super().__init__(f"Unparseable timestamp {value!r}; expected {expected}")


_MDY_FORMAT = "MM/DD/YY HH:MM:SS"
_ISO_FORMAT = "an ISO 8601 date-time"

_MDY_RE = re.compile(r"(\d{2})/(\d{2})/(\d{2}) (\d{2}):(\d{2}):(\d{2})")


def parse_mdy_datetime(text: object) -> datetime:
    """Parse ``MM/DD/YY HH:MM:SS`` into a naive ``datetime`` (years 2000-2099).

    Surrounding whitespace is ignored. Raises ``TimestampParseError`` for
    anything else, including impossible dates such as ``02/30/24``.
    """
    if not isinstance(text, str):
        raise TimestampParseError(text, _MDY_FORMAT)
    match = _MDY_RE.fullmatch(text.strip())
    if match is None:
        raise TimestampParseError(text, _MDY_FORMAT)
    month, day, yy, hour, minute, second = (int(g) for g in match.groups())
    try:
        return datetime(2000 + yy, month, day, hour, minute, second)
    except ValueError:
        raise TimestampParseError(text, _MDY_FORMAT) from None


def parse_iso_datetime(text: object) -> datetime:
    """Parse an ISO 8601 date-time, keeping any explicit offset.

    Returns a naive ``datetime`` when the text has no offset and an aware one
    when it has ``±HH:MM`` (or another ISO offset form) or ``Z``. Fractional
    seconds are accepted. Surrounding whitespace is ignored. Raises
    ``TimestampParseError`` for empty, non-string, or non-ISO input.
    """
    if not isinstance(text, str):
        raise TimestampParseError(text, _ISO_FORMAT)
    stripped = text.strip()
    if not stripped:
        raise TimestampParseError(text, _ISO_FORMAT)
    try:
        return datetime.fromisoformat(stripped)
    except ValueError:
        raise TimestampParseError(text, _ISO_FORMAT) from None


def _localize(
    dt: datetime,
    tz: ZoneInfo,
    file_name: str | None,
    counter: DstAdjustmentCounter | None,
) -> datetime:
    if counter is not None and file_name is not None:
        return counter.localize(file_name, dt, tz)
    return localize(dt, tz).value


def localize_mdy(
    text: object,
    tz: ZoneInfo,
    file_name: str | None = None,
    counter: DstAdjustmentCounter | None = None,
) -> datetime:
    """Parse ``MM/DD/YY HH:MM:SS`` and interpret it in the Source_Timezone ``tz``.

    Returns a timezone-aware ``datetime``. DST adjustments are counted against
    ``file_name`` when both ``file_name`` and ``counter`` are given. Raises
    ``TimestampParseError`` when the text can't be parsed (nothing is counted).
    """
    return _localize(parse_mdy_datetime(text), tz, file_name, counter)


def localize_iso(
    text: object,
    tz: ZoneInfo,
    file_name: str | None = None,
    counter: DstAdjustmentCounter | None = None,
) -> datetime:
    """Parse an ISO 8601 date-time and return it timezone-aware.

    An explicit offset wins over ``tz`` (Requirement 9.3); otherwise the wall
    time is interpreted in ``tz`` with DST handling (9.1, 9.4), counted against
    ``file_name`` when both ``file_name`` and ``counter`` are given. Raises
    ``TimestampParseError`` when the text can't be parsed.
    """
    return _localize(parse_iso_datetime(text), tz, file_name, counter)
