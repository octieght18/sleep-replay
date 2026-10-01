"""SensorPush_CSV timestamp parsing (Requirements 6.6, 9.3, 9.4).

Supported formats (Input Format Assumptions):

* ISO 8601: ``YYYY-MM-DD HH:MM[:SS]`` and ``YYYY-MM-DDTHH:MM[:SS]``, each
  optionally followed by an explicit offset ``±HH:MM`` or ``Z``. Fractional
  seconds (``:SS.ffffff``) are tolerated. ``T`` and ``Z`` are case-insensitive.
* US 12-hour: ``M/D/YYYY h:mm[:ss] AM/PM`` (``AM``/``PM`` case-insensitive,
  the space before it optional).
* US 24-hour: ``M/D/YYYY HH:MM[:SS]``.

Slash-separated dates are always month/day/year, so ``13/1/2024`` is invalid
rather than being read day-first (day-first dates are unsupported in the MVP).
Surrounding whitespace is ignored.

Failure signalling: parse failures raise ``InvalidTimestamp`` (a
``ValueError`` subclass). The SensorPush_Importer catches it, skips the row,
and increments the skipped-row count (Requirement 6.8). An empty cell raises
``InvalidTimestamp`` as well.

Localization: an explicit offset wins; an offset-free timestamp is interpreted
in the SensorPush Source_Timezone via ``domain.timezones.localize``, which
resolves DST-ambiguous times to the earlier instant and shifts nonexistent
times forward by the gap (9.3, 9.4). Pass the ``Localized`` result to
``DstAdjustmentCounter.record`` to get the per-file DST warning.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Final
from zoneinfo import ZoneInfo

from backend.domain.timezones import Localized, localize

__all__ = [
    "SUPPORTED_FORMATS",
    "InvalidTimestamp",
    "parse_timestamp",
    "parse_and_localize",
]

#: Human-readable list of the supported formats, for User_Error messages such
#: as ``NO_READABLE_ROWS`` (Requirement 6.9).
SUPPORTED_FORMATS: Final[tuple[str, ...]] = (
    "YYYY-MM-DD HH:MM[:SS]",
    "YYYY-MM-DDTHH:MM[:SS][±HH:MM|Z]",
    "M/D/YYYY h:mm[:ss] AM/PM",
    "M/D/YYYY HH:MM[:SS]",
)


class InvalidTimestamp(ValueError):
    """A SensorPush timestamp cell is empty or matches no supported format."""

    def __init__(self, text: object) -> None:
        super().__init__(f"Unsupported SensorPush timestamp: {text!r}")
        self.text = text


_ISO_RE: Final = re.compile(
    r"(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})"
    r"(?:[Tt]|\s+)"
    r"(?P<hour>\d{2}):(?P<minute>\d{2})"
    r"(?::(?P<second>\d{2})(?:\.(?P<fraction>\d{1,6}))?)?"
    r"\s*(?P<offset>[Zz]|[+-]\d{2}:\d{2})?",
    re.ASCII,
)

_US_RE: Final = re.compile(
    r"(?P<month>\d{1,2})/(?P<day>\d{1,2})/(?P<year>\d{4})"
    r"\s+"
    r"(?P<hour>\d{1,2}):(?P<minute>\d{2})"
    r"(?::(?P<second>\d{2}))?"
    r"(?:\s*(?P<ampm>[AaPp][Mm]))?",
    re.ASCII,
)


def _offset(token: str | None) -> timezone | None:
    if token is None:
        return None
    if token in ("Z", "z"):
        return timezone.utc
    sign = -1 if token[0] == "-" else 1
    hours, minutes = int(token[1:3]), int(token[4:6])
    if minutes > 59:
        raise ValueError("offset minutes out of range")
    # timezone() rejects offsets of 24h or more.
    return timezone(sign * timedelta(hours=hours, minutes=minutes))


def _parse_iso(m: re.Match[str]) -> datetime:
    fraction = m["fraction"]
    return datetime(
        int(m["year"]),
        int(m["month"]),
        int(m["day"]),
        int(m["hour"]),
        int(m["minute"]),
        int(m["second"] or 0),
        int(fraction.ljust(6, "0")) if fraction else 0,
        tzinfo=_offset(m["offset"]),
    )


def _parse_us(m: re.Match[str]) -> datetime:
    hour = int(m["hour"])
    ampm = m["ampm"]
    if ampm is not None:
        if not 1 <= hour <= 12:
            raise ValueError("12-hour clock hour out of range")
        hour = hour % 12 + (12 if ampm.lower() == "pm" else 0)
    return datetime(
        int(m["year"]),  # slash dates are always month/day/year
        int(m["month"]),
        int(m["day"]),
        hour,
        int(m["minute"]),
        int(m["second"] or 0),
    )


def parse_timestamp(text: object) -> datetime:
    """Parse one SensorPush timestamp cell.

    Returns a timezone-aware ``datetime`` (fixed offset) when the text has an
    explicit offset, otherwise a naive ``datetime`` holding the wall time.

    Raises:
        InvalidTimestamp: the cell is empty, not a string, or matches none of
            ``SUPPORTED_FORMATS`` (including impossible dates such as
            ``13/1/2024`` or ``2024-02-30 00:00``).
    """
    if not isinstance(text, str):
        raise InvalidTimestamp(text)
    stripped = text.strip()
    try:
        if m := _ISO_RE.fullmatch(stripped):
            return _parse_iso(m)
        if m := _US_RE.fullmatch(stripped):
            return _parse_us(m)
    except ValueError:  # out-of-range field (month 13, hour 25, ...)
        pass
    raise InvalidTimestamp(text)


def parse_and_localize(text: object, source_tz: ZoneInfo) -> Localized:
    """Parse a SensorPush timestamp and make it timezone-aware.

    An explicit offset is kept and ``source_tz`` is ignored (9.3); otherwise the
    wall time is interpreted in ``source_tz`` with the DST rules of
    ``domain.timezones.localize`` (6.6, 9.4). Feed the result to
    ``DstAdjustmentCounter.record(file_name, result)`` to count adjustments.

    Raises:
        InvalidTimestamp: as for ``parse_timestamp``.
    """
    return localize(parse_timestamp(text), source_tz)
