"""Timezone localization helpers shared by the Importers.

This module lives in ``domain`` (and uses only the standard library's
``zoneinfo``) so that the Importers can apply Source_Timezones and DST rules
without importing ``processing`` (Dependency_Rule (b)). Display_Timezone
resolution and UTC conversion for alignment live in ``processing.timezones``.

Rules implemented here (Requirement 9):

* Every file type has a Source_Timezone: the user's IANA override for that
  file type, otherwise the default from the Input Format Assumptions
  (Fitbit intraday heart rate and steps: UTC; every other file type: the
  Display_Timezone in effect when the import starts) (9.1, 9.2).
* A timestamp with an explicit offset (``±HH:MM`` or ``Z``) keeps that offset;
  the Source_Timezone is ignored for it (9.3).
* An offset-free timestamp that is ambiguous in the Source_Timezone (repeated
  hour) resolves to its earlier UTC instant; one that is nonexistent (skipped
  hour) is shifted forward by the length of the gap, e.g. 02:30 -> 03:30 for a
  1-hour gap. Each file gets at most one warning stating how many of its
  timestamps were adjusted (9.4).
* An override that is not a valid IANA identifier is rejected with an
  ``INVALID_TIMEZONE`` User_Error naming the value and file type (9.10).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from functools import lru_cache
from typing import Final, Literal, NamedTuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

from backend.domain.errors import (
    INVALID_TIMEZONE,
    NO_ACTION_REQUIRED,
    User_Error,
    Warning_Item,
)

# ---------------------------------------------------------------------------
# File types
# ---------------------------------------------------------------------------

#: Fitbit ``sleep-YYYY-MM-DD.json`` (sleep logs and stage levels).
FITBIT_SLEEP: Final = "fitbit_sleep"
#: Fitbit ``heart_rate-YYYY-MM-DD.json`` (intraday heart rate).
FITBIT_HEART_RATE: Final = "fitbit_heart_rate"
#: Fitbit ``steps-YYYY-MM-DD.json`` (per-minute steps).
FITBIT_STEPS: Final = "fitbit_steps"
#: Fitbit ``Heart Rate Variability Details - YYYY-MM-DD.csv``.
FITBIT_HRV_DETAILS: Final = "fitbit_hrv_details"
#: Fitbit ``Daily Heart Rate Variability Summary - YYYY-MM(-DD).csv``.
FITBIT_HRV_SUMMARY: Final = "fitbit_hrv_summary"
#: A SensorPush CSV export.
SENSORPUSH: Final = "sensorpush"

#: Every file type that has a Source_Timezone, in documentation order.
FILE_TYPES: Final[tuple[str, ...]] = (
    FITBIT_SLEEP,
    FITBIT_HEART_RATE,
    FITBIT_STEPS,
    FITBIT_HRV_DETAILS,
    FITBIT_HRV_SUMMARY,
    SENSORPUSH,
)

#: Sentinel default meaning "the Display_Timezone in effect when the import starts".
DISPLAY_TIMEZONE: Final = "Display_Timezone"

#: Default Source_Timezone per file type (Input Format Assumptions). The UTC
#: default for Fitbit intraday heart rate and steps reflects known export
#: behavior; every default is user-overridable per file type.
DEFAULT_SOURCE_TIMEZONES: Final[Mapping[str, str]] = {
    FITBIT_SLEEP: DISPLAY_TIMEZONE,
    FITBIT_HEART_RATE: "UTC",
    FITBIT_STEPS: "UTC",
    FITBIT_HRV_DETAILS: DISPLAY_TIMEZONE,
    FITBIT_HRV_SUMMARY: DISPLAY_TIMEZONE,
    SENSORPUSH: DISPLAY_TIMEZONE,
}


# ---------------------------------------------------------------------------
# IANA validation and Source_Timezone resolution
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _known_timezones() -> frozenset[str]:
    return frozenset(available_timezones())


def validate_iana(name: object, file_type: str | None = None) -> ZoneInfo:
    """Return the ``ZoneInfo`` for an IANA timezone identifier.

    The identifier must match a known zone exactly (case-sensitive), so the
    result doesn't depend on the host file system's case sensitivity.

    Raises:
        User_Error: with code ``INVALID_TIMEZONE`` naming the invalid value and,
            when given, the affected file type (Requirement 9.10).
    """
    if isinstance(name, str) and name:
        known = _known_timezones()
        # An empty zone list means tz data enumeration is unavailable; fall back
        # to whether ZoneInfo can load the key at all.
        if not known or name in known:
            try:
                return ZoneInfo(name)
            except (ZoneInfoNotFoundError, ValueError, OSError):
                pass
    subject = f" for {file_type} files" if file_type else ""
    raise User_Error(
        code=INVALID_TIMEZONE,
        description=(
            f"The timezone {name!r}{subject} is not a valid IANA timezone identifier. "
            "Nothing from this import was saved."
        ),
        action=(
            "Enter an IANA timezone identifier such as 'UTC', 'Europe/Berlin', or "
            "'America/New_York', then import the files again."
        ),
        details={"value": str(name), **({"file_type": file_type} if file_type else {})},
    )


def validate_overrides(overrides: Mapping[str, str] | None) -> dict[str, ZoneInfo]:
    """Validate every user Source_Timezone override before anything is imported.

    Returns the overrides resolved to ``ZoneInfo`` objects. Raises
    ``INVALID_TIMEZONE`` for the first invalid value (in ``FILE_TYPES`` order)
    and ``ValueError`` for a key that isn't a known file type.
    """
    if not overrides:
        return {}
    unknown = sorted(set(overrides) - set(FILE_TYPES))
    if unknown:
        raise ValueError(f"Unknown file type(s) in Source_Timezone overrides: {unknown}")
    return {ft: validate_iana(overrides[ft], ft) for ft in FILE_TYPES if ft in overrides}


def resolve_source_timezone(
    file_type: str,
    overrides: Mapping[str, str] | None,
    display_tz: ZoneInfo | str,
) -> ZoneInfo:
    """Return the Source_Timezone applied to ``file_type`` (Requirement 9.1, 9.2).

    Precedence: the user override for this file type, then the default from
    ``DEFAULT_SOURCE_TIMEZONES``, where ``DISPLAY_TIMEZONE`` means
    ``display_tz`` (the Display_Timezone in effect when the import starts).
    Use ``.key`` on the result to record the applied IANA name in the
    Import_Report.

    Raises:
        ValueError: ``file_type`` isn't one of ``FILE_TYPES``.
        User_Error: ``INVALID_TIMEZONE`` when the override is not a valid IANA
            identifier.
    """
    if file_type not in DEFAULT_SOURCE_TIMEZONES:
        raise ValueError(f"Unknown file type: {file_type!r}")
    if overrides and file_type in overrides:
        return validate_iana(overrides[file_type], file_type)
    default = DEFAULT_SOURCE_TIMEZONES[file_type]
    if default == DISPLAY_TIMEZONE:
        return display_tz if isinstance(display_tz, ZoneInfo) else validate_iana(display_tz)
    return ZoneInfo(default)


# ---------------------------------------------------------------------------
# Localization with DST handling
# ---------------------------------------------------------------------------

Adjustment = Literal["ambiguous", "nonexistent"]


class Localized(NamedTuple):
    """Result of ``localize``: the timezone-aware value and any DST adjustment."""

    value: datetime
    #: ``"ambiguous"`` (repeated hour, earlier instant chosen), ``"nonexistent"``
    #: (skipped hour, shifted forward by the gap), or ``None``.
    adjustment: Adjustment | None = None

    @property
    def adjusted(self) -> bool:
        return self.adjustment is not None


def localize(dt: datetime, tz: ZoneInfo) -> Localized:
    """Attach a Source_Timezone to a parsed timestamp.

    * ``dt`` already timezone-aware (explicit offset in the source): returned
      unchanged; ``tz`` is ignored (Requirement 9.3).
    * Offset-free and unambiguous: interpreted as wall time in ``tz``.
    * Ambiguous (repeated hour): the earlier UTC instant (Requirement 9.4).
    * Nonexistent (skipped hour): shifted forward by the gap length, so 02:30
      becomes 03:30 across a 1-hour spring-forward gap (Requirement 9.4).

    The returned value always carries ``tz`` (or the explicit offset) as its
    tzinfo; convert to UTC with ``.astimezone(timezone.utc)``.
    """
    if dt.tzinfo is not None and dt.utcoffset() is not None:
        return Localized(dt)

    naive = dt.replace(tzinfo=None, fold=0)
    first = naive.replace(tzinfo=tz, fold=0)
    second = naive.replace(tzinfo=tz, fold=1)
    if first.utcoffset() == second.utcoffset():
        return Localized(first)

    first_utc = first.astimezone(timezone.utc)
    if first_utc.astimezone(tz).replace(tzinfo=None, fold=0) != naive:
        # Skipped wall time. With fold=0, zoneinfo applies the offset in effect
        # before the transition; converting that instant back to ``tz`` yields
        # the wall time moved forward by exactly the gap length.
        return Localized(first_utc.astimezone(tz), "nonexistent")

    # Repeated wall time: pick whichever fold maps to the earlier UTC instant.
    second_utc = second.astimezone(timezone.utc)
    return Localized(first if first_utc <= second_utc else second, "ambiguous")


class DstAdjustmentCounter:
    """Counts DST-adjusted timestamps per file and yields one warning per file.

    Typical use inside an Importer::

        counter = DstAdjustmentCounter()
        ts = counter.localize(file_name, parsed, source_tz)
        ...
        report.warnings.extend(counter.warnings())
    """

    def __init__(self) -> None:
        self._counts: dict[str, int] = {}

    def record(self, file_name: str, result: Localized) -> datetime:
        """Count ``result`` against ``file_name`` if it was adjusted; return its value."""
        if result.adjusted:
            self._counts[file_name] = self._counts.get(file_name, 0) + 1
        return result.value

    def localize(self, file_name: str, dt: datetime, tz: ZoneInfo) -> datetime:
        """``localize(dt, tz)`` and count any adjustment against ``file_name``."""
        return self.record(file_name, localize(dt, tz))

    def count(self, file_name: str) -> int:
        return self._counts.get(file_name, 0)

    @property
    def counts(self) -> dict[str, int]:
        """Adjusted-timestamp counts for files with at least one adjustment."""
        return dict(self._counts)

    def warnings(self) -> list[Warning_Item]:
        """One Warning_Item per affected file, ordered by file name (Requirement 9.4)."""
        items: list[Warning_Item] = []
        for file_name in sorted(self._counts):
            n = self._counts[file_name]
            noun, verb = ("timestamp", "was") if n == 1 else ("timestamps", "were")
            items.append(
                Warning_Item(
                    description=(
                        f"{n} {noun} in {file_name} fell in a daylight saving time "
                        f"transition and {verb} adjusted: times in a repeated hour were "
                        "read as the earlier occurrence, and times in a skipped hour "
                        "were moved forward by the length of the gap."
                    ),
                    subject=file_name,
                    recommended_action=NO_ACTION_REQUIRED,
                )
            )
        return items
