"""Display_Timezone resolution and UTC conversion (Requirement 9.5, 9.6, 9.8, 9.11).

Configuration
-------------
The Display_Timezone is read once at Backend startup from the environment
variable ``SLEEP_REPLAY_DISPLAY_TIMEZONE`` (:data:`DISPLAY_TIMEZONE_ENV`). Its
value must be an IANA timezone identifier such as ``Europe/Berlin`` or
``America/New_York``. Set it in the shell before starting the Backend for the
non-Docker setup (``export SLEEP_REPLAY_DISPLAY_TIMEZONE=Europe/Berlin`` or, in
PowerShell, ``$env:SLEEP_REPLAY_DISPLAY_TIMEZONE = "Europe/Berlin"``), or in the
``environment`` section / ``.env`` file used by ``docker compose up``.

Resolution order (:func:`resolve_display_timezone`):

1. The configured value, when set and non-blank. An invalid identifier gives
   UTC plus a Warning_Item; it does not fall through to the host timezone
   (Requirement 9.11).
2. Otherwise the host machine's timezone (:func:`detect_host_timezone`): the
   ``TZ`` environment variable, then ``/etc/timezone`` and the
   ``/etc/localtime`` symlink on Linux/macOS, then the Windows registry
   timezone (or ``time.tzname``) mapped to IANA through the CLDR table.
3. Otherwise UTC plus a Warning_Item stating the reason and how to configure
   the Display_Timezone.

UTC conversion
--------------
All processing is keyed on UTC instants (Requirement 9.5). :func:`to_utc`
converts timezone-aware values, and localizes offset-free values in a given
timezone first (with the DST rules of ``backend.domain.timezones.localize``).
Elapsed time is always computed between UTC instants (:func:`elapsed_seconds`),
so a 22:00 -> 06:00 local session lasts 9 h across a 1-hour fall-back
transition and 7 h across a spring-forward one (Requirement 9.8). Python's
subtraction of two aware datetimes that share a ``ZoneInfo`` compares wall
times, which is why these helpers convert to UTC first.

Only the standard library and ``backend.domain`` are imported here.
"""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Final, Literal
from zoneinfo import ZoneInfo

from backend.domain.errors import User_Error, Warning_Item
from backend.domain.timezones import localize, validate_iana

__all__ = [
    "DISPLAY_TIMEZONE_ENV",
    "UTC_ZONE",
    "WINDOWS_TO_IANA",
    "DisplayTimezone",
    "DisplayTimezoneSource",
    "resolve_display_timezone",
    "detect_host_timezone",
    "windows_zone_to_iana",
    "to_utc",
    "to_display",
    "utc_epoch_seconds",
    "elapsed_seconds",
    "add_elapsed_seconds",
]

#: Environment variable that configures the Display_Timezone at Backend startup.
DISPLAY_TIMEZONE_ENV: Final = "SLEEP_REPLAY_DISPLAY_TIMEZONE"

#: The IANA ``UTC`` zone, used as the fallback Display_Timezone.
UTC_ZONE: Final[ZoneInfo] = ZoneInfo("UTC")

_WARNING_SUBJECT: Final = "Display_Timezone"

_CONFIGURE_ACTION: Final = (
    f"Set the {DISPLAY_TIMEZONE_ENV} setting to an IANA timezone identifier such as "
    "'Europe/Berlin' or 'America/New_York' (in the environment for the non-Docker "
    "setup, or in the environment/.env file used by 'docker compose up'), then "
    "restart the Backend."
)

DisplayTimezoneSource = Literal["config", "host", "fallback"]


@dataclass(frozen=True)
class DisplayTimezone:
    """The Display_Timezone in effect and how it was determined.

    Attributes:
        zone: The IANA timezone.
        source: ``"config"`` (from :data:`DISPLAY_TIMEZONE_ENV`), ``"host"``
            (host machine timezone), or ``"fallback"`` (UTC because the
            configured value was invalid or the host timezone was unknown).
        warning: The Warning_Item to show when ``source == "fallback"``,
            otherwise ``None``.
    """

    zone: ZoneInfo
    source: DisplayTimezoneSource
    warning: Warning_Item | None = None

    @property
    def name(self) -> str:
        """The IANA identifier, for display in the Settings_Panel."""
        return self.zone.key


# ---------------------------------------------------------------------------
# Display_Timezone resolution
# ---------------------------------------------------------------------------


def resolve_display_timezone(
    environ: Mapping[str, str] | None = None,
    host_detector: Callable[[], str | None] | None = None,
) -> DisplayTimezone:
    """Resolve the Display_Timezone (Requirement 9.6, 9.11). Call at startup.

    Args:
        environ: Environment to read; defaults to ``os.environ``.
        host_detector: Returns the host IANA name or ``None``; defaults to
            ``detect_host_timezone(environ)``.
    """
    env = os.environ if environ is None else environ
    configured = (env.get(DISPLAY_TIMEZONE_ENV) or "").strip()

    if configured:
        try:
            return DisplayTimezone(validate_iana(configured), "config")
        except User_Error:
            return DisplayTimezone(
                UTC_ZONE,
                "fallback",
                Warning_Item(
                    description=(
                        f"The configured Display_Timezone {configured!r} is not a valid IANA "
                        "timezone identifier, so times are shown in UTC."
                    ),
                    subject=_WARNING_SUBJECT,
                    recommended_action=_CONFIGURE_ACTION,
                ),
            )

    detector = host_detector if host_detector is not None else (lambda: detect_host_timezone(env))
    host_name = detector()
    if host_name:
        try:
            return DisplayTimezone(validate_iana(host_name), "host")
        except User_Error:
            pass

    return DisplayTimezone(
        UTC_ZONE,
        "fallback",
        Warning_Item(
            description=(
                "No Display_Timezone is configured and the host machine's timezone could "
                "not be determined, so times are shown in UTC."
            ),
            subject=_WARNING_SUBJECT,
            recommended_action=_CONFIGURE_ACTION,
        ),
    )


# ---------------------------------------------------------------------------
# Host timezone detection (best effort, stdlib only)
# ---------------------------------------------------------------------------


def _is_valid(name: str | None) -> bool:
    if not name:
        return False
    try:
        validate_iana(name)
    except User_Error:
        return False
    return True


def _name_from_zoneinfo_path(path: str) -> str | None:
    """``/usr/share/zoneinfo/Europe/Berlin`` -> ``Europe/Berlin``."""
    normalized = path.replace("\\", "/")
    marker = "zoneinfo/"
    idx = normalized.rfind(marker)
    if idx < 0:
        return None
    candidate = normalized[idx + len(marker) :]
    # Skip the "posix/" and "right/" variant trees.
    for prefix in ("posix/", "right/"):
        if candidate.startswith(prefix):
            candidate = candidate[len(prefix) :]
    return candidate or None


def _from_tz_env(environ: Mapping[str, str]) -> str | None:
    value = (environ.get("TZ") or "").strip()
    if not value:
        return None
    if value.startswith(":"):
        value = value[1:]
    if _is_valid(value):
        return value
    from_path = _name_from_zoneinfo_path(value)
    return from_path if _is_valid(from_path) else None


def _from_posix_files(
    timezone_file: Path = Path("/etc/timezone"),
    localtime: Path = Path("/etc/localtime"),
) -> str | None:
    try:
        text = timezone_file.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        text = ""
    if _is_valid(text):
        return text
    try:
        if localtime.is_symlink():
            name = _name_from_zoneinfo_path(os.path.realpath(localtime))
            if not _is_valid(name):
                name = _name_from_zoneinfo_path(os.readlink(localtime))
            if _is_valid(name):
                return name
    except OSError:
        pass
    return None


def _windows_registry_zone() -> str | None:
    try:
        import winreg  # type: ignore[import-not-found]
    except ImportError:
        return None
    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Control\TimeZoneInformation",
        ) as key:
            value, _ = winreg.QueryValueEx(key, "TimeZoneKeyName")
    except OSError:
        return None
    # Some Windows builds pad the value with NUL characters.
    return str(value).split("\x00", 1)[0].strip() or None


def _from_windows() -> str | None:
    iana = windows_zone_to_iana(_windows_registry_zone())
    if _is_valid(iana):
        return iana
    # time.tzname holds the (usually English) standard/daylight names, which
    # match the registry key names for most zones.
    for name in time.tzname:
        iana = windows_zone_to_iana(name)
        if _is_valid(iana):
            return iana
    return None


def detect_host_timezone(environ: Mapping[str, str] | None = None) -> str | None:
    """Return the host machine's IANA timezone name, or ``None`` if unknown.

    Checks, in order: the ``TZ`` environment variable (an IANA name, optionally
    prefixed with ``:``, or a path inside a ``zoneinfo`` tree); on Windows, the
    registry timezone key name (then ``time.tzname``) mapped through
    :data:`WINDOWS_TO_IANA`; elsewhere ``/etc/timezone`` and the
    ``/etc/localtime`` symlink target.
    """
    env = os.environ if environ is None else environ
    name = _from_tz_env(env)
    if name:
        return name
    if sys.platform == "win32":
        return _from_windows()
    return _from_posix_files()


# CLDR windowsZones.xml, territory "001" (the primary IANA zone per Windows zone).
# Canonical current names are used where tzdata renamed a zone.
WINDOWS_TO_IANA: Final[Mapping[str, str]] = {
    "Dateline Standard Time": "Etc/GMT+12",
    "UTC-11": "Etc/GMT+11",
    "Aleutian Standard Time": "America/Adak",
    "Hawaiian Standard Time": "Pacific/Honolulu",
    "Marquesas Standard Time": "Pacific/Marquesas",
    "Alaskan Standard Time": "America/Anchorage",
    "UTC-09": "Etc/GMT+9",
    "Pacific Standard Time (Mexico)": "America/Tijuana",
    "UTC-08": "Etc/GMT+8",
    "Pacific Standard Time": "America/Los_Angeles",
    "US Mountain Standard Time": "America/Phoenix",
    "Mountain Standard Time (Mexico)": "America/Mazatlan",
    "Mountain Standard Time": "America/Denver",
    "Yukon Standard Time": "America/Whitehorse",
    "Central America Standard Time": "America/Guatemala",
    "Central Standard Time": "America/Chicago",
    "Easter Island Standard Time": "Pacific/Easter",
    "Central Standard Time (Mexico)": "America/Mexico_City",
    "Canada Central Standard Time": "America/Regina",
    "SA Pacific Standard Time": "America/Bogota",
    "Eastern Standard Time (Mexico)": "America/Cancun",
    "Eastern Standard Time": "America/New_York",
    "Haiti Standard Time": "America/Port-au-Prince",
    "Cuba Standard Time": "America/Havana",
    "US Eastern Standard Time": "America/Indiana/Indianapolis",
    "Turks And Caicos Standard Time": "America/Grand_Turk",
    "Paraguay Standard Time": "America/Asuncion",
    "Atlantic Standard Time": "America/Halifax",
    "Venezuela Standard Time": "America/Caracas",
    "Central Brazilian Standard Time": "America/Cuiaba",
    "SA Western Standard Time": "America/La_Paz",
    "Pacific SA Standard Time": "America/Santiago",
    "Newfoundland Standard Time": "America/St_Johns",
    "Tocantins Standard Time": "America/Araguaina",
    "E. South America Standard Time": "America/Sao_Paulo",
    "SA Eastern Standard Time": "America/Cayenne",
    "Argentina Standard Time": "America/Argentina/Buenos_Aires",
    "Greenland Standard Time": "America/Nuuk",
    "Montevideo Standard Time": "America/Montevideo",
    "Magallanes Standard Time": "America/Punta_Arenas",
    "Saint Pierre Standard Time": "America/Miquelon",
    "Bahia Standard Time": "America/Bahia",
    "UTC-02": "Etc/GMT+2",
    "Mid-Atlantic Standard Time": "Etc/GMT+2",
    "Azores Standard Time": "Atlantic/Azores",
    "Cape Verde Standard Time": "Atlantic/Cape_Verde",
    "UTC": "Etc/UTC",
    "Coordinated Universal Time": "Etc/UTC",
    "GMT Standard Time": "Europe/London",
    "Greenwich Standard Time": "Atlantic/Reykjavik",
    "Sao Tome Standard Time": "Africa/Sao_Tome",
    "Morocco Standard Time": "Africa/Casablanca",
    "W. Europe Standard Time": "Europe/Berlin",
    "Central Europe Standard Time": "Europe/Budapest",
    "Romance Standard Time": "Europe/Paris",
    "Central European Standard Time": "Europe/Warsaw",
    "W. Central Africa Standard Time": "Africa/Lagos",
    "Jordan Standard Time": "Asia/Amman",
    "GTB Standard Time": "Europe/Bucharest",
    "Middle East Standard Time": "Asia/Beirut",
    "Egypt Standard Time": "Africa/Cairo",
    "E. Europe Standard Time": "Europe/Chisinau",
    "Syria Standard Time": "Asia/Damascus",
    "West Bank Standard Time": "Asia/Hebron",
    "South Africa Standard Time": "Africa/Johannesburg",
    "FLE Standard Time": "Europe/Kyiv",
    "Israel Standard Time": "Asia/Jerusalem",
    "South Sudan Standard Time": "Africa/Juba",
    "Kaliningrad Standard Time": "Europe/Kaliningrad",
    "Sudan Standard Time": "Africa/Khartoum",
    "Libya Standard Time": "Africa/Tripoli",
    "Namibia Standard Time": "Africa/Windhoek",
    "Arabic Standard Time": "Asia/Baghdad",
    "Turkey Standard Time": "Europe/Istanbul",
    "Arab Standard Time": "Asia/Riyadh",
    "Belarus Standard Time": "Europe/Minsk",
    "Russian Standard Time": "Europe/Moscow",
    "E. Africa Standard Time": "Africa/Nairobi",
    "Volgograd Standard Time": "Europe/Volgograd",
    "Iran Standard Time": "Asia/Tehran",
    "Arabian Standard Time": "Asia/Dubai",
    "Astrakhan Standard Time": "Europe/Astrakhan",
    "Azerbaijan Standard Time": "Asia/Baku",
    "Russia Time Zone 3": "Europe/Samara",
    "Mauritius Standard Time": "Indian/Mauritius",
    "Saratov Standard Time": "Europe/Saratov",
    "Georgian Standard Time": "Asia/Tbilisi",
    "Caucasus Standard Time": "Asia/Yerevan",
    "Afghanistan Standard Time": "Asia/Kabul",
    "West Asia Standard Time": "Asia/Tashkent",
    "Ekaterinburg Standard Time": "Asia/Yekaterinburg",
    "Pakistan Standard Time": "Asia/Karachi",
    "Qyzylorda Standard Time": "Asia/Qyzylorda",
    "India Standard Time": "Asia/Kolkata",
    "Sri Lanka Standard Time": "Asia/Colombo",
    "Nepal Standard Time": "Asia/Kathmandu",
    "Central Asia Standard Time": "Asia/Almaty",
    "Bangladesh Standard Time": "Asia/Dhaka",
    "Omsk Standard Time": "Asia/Omsk",
    "Myanmar Standard Time": "Asia/Yangon",
    "SE Asia Standard Time": "Asia/Bangkok",
    "Altai Standard Time": "Asia/Barnaul",
    "W. Mongolia Standard Time": "Asia/Hovd",
    "North Asia Standard Time": "Asia/Krasnoyarsk",
    "N. Central Asia Standard Time": "Asia/Novosibirsk",
    "Tomsk Standard Time": "Asia/Tomsk",
    "China Standard Time": "Asia/Shanghai",
    "North Asia East Standard Time": "Asia/Irkutsk",
    "Singapore Standard Time": "Asia/Singapore",
    "W. Australia Standard Time": "Australia/Perth",
    "Taipei Standard Time": "Asia/Taipei",
    "Ulaanbaatar Standard Time": "Asia/Ulaanbaatar",
    "Aus Central W. Standard Time": "Australia/Eucla",
    "Transbaikal Standard Time": "Asia/Chita",
    "Tokyo Standard Time": "Asia/Tokyo",
    "North Korea Standard Time": "Asia/Pyongyang",
    "Korea Standard Time": "Asia/Seoul",
    "Yakutsk Standard Time": "Asia/Yakutsk",
    "Cen. Australia Standard Time": "Australia/Adelaide",
    "AUS Central Standard Time": "Australia/Darwin",
    "E. Australia Standard Time": "Australia/Brisbane",
    "AUS Eastern Standard Time": "Australia/Sydney",
    "West Pacific Standard Time": "Pacific/Port_Moresby",
    "Tasmania Standard Time": "Australia/Hobart",
    "Vladivostok Standard Time": "Asia/Vladivostok",
    "Lord Howe Standard Time": "Australia/Lord_Howe",
    "Bougainville Standard Time": "Pacific/Bougainville",
    "Russia Time Zone 10": "Asia/Srednekolymsk",
    "Magadan Standard Time": "Asia/Magadan",
    "Norfolk Standard Time": "Pacific/Norfolk",
    "Sakhalin Standard Time": "Asia/Sakhalin",
    "Central Pacific Standard Time": "Pacific/Guadalcanal",
    "Russia Time Zone 11": "Asia/Kamchatka",
    "New Zealand Standard Time": "Pacific/Auckland",
    "UTC+12": "Etc/GMT-12",
    "Fiji Standard Time": "Pacific/Fiji",
    "Kamchatka Standard Time": "Asia/Kamchatka",
    "Chatham Islands Standard Time": "Pacific/Chatham",
    "UTC+13": "Etc/GMT-13",
    "Tonga Standard Time": "Pacific/Tongatapu",
    "Samoa Standard Time": "Pacific/Apia",
    "Line Islands Standard Time": "Pacific/Kiritimati",
}

# Lookup that also accepts the daylight-time display names ("... Daylight Time").
_WINDOWS_LOOKUP: Final[dict[str, str]] = {k.casefold(): v for k, v in WINDOWS_TO_IANA.items()}
_WINDOWS_LOOKUP.update(
    {
        k.replace("Standard Time", "Daylight Time").casefold(): v
        for k, v in WINDOWS_TO_IANA.items()
        if "Standard Time" in k
    }
)


def windows_zone_to_iana(name: str | None) -> str | None:
    """Map a Windows timezone ID (or its daylight-time name) to an IANA name.

    Matching is case-insensitive; unknown names give ``None``.
    """
    if not name:
        return None
    return _WINDOWS_LOOKUP.get(name.strip().casefold())


# ---------------------------------------------------------------------------
# UTC conversion and elapsed time
# ---------------------------------------------------------------------------

_EPOCH: Final = datetime(1970, 1, 1, tzinfo=timezone.utc)


def to_utc(dt: datetime, tz: ZoneInfo | None = None) -> datetime:
    """Return ``dt`` as a UTC instant (``tzinfo=timezone.utc``) (Requirement 9.5).

    A timezone-aware ``dt`` is converted directly and ``tz`` is ignored. An
    offset-free ``dt`` is first interpreted in ``tz`` using the DST rules of
    :func:`backend.domain.timezones.localize` (ambiguous -> earlier instant,
    nonexistent -> shifted forward by the gap).

    Raises:
        ValueError: ``dt`` is offset-free and no ``tz`` was given.
    """
    if dt.tzinfo is None or dt.utcoffset() is None:
        if tz is None:
            raise ValueError("an offset-free datetime needs a timezone to convert to UTC")
        dt = localize(dt, tz).value
    return dt.astimezone(timezone.utc)


def to_display(dt: datetime, display_tz: ZoneInfo | DisplayTimezone) -> datetime:
    """Convert an aware instant to the Display_Timezone, using the UTC offset in
    effect at that instant (Requirement 9.7)."""
    zone = display_tz.zone if isinstance(display_tz, DisplayTimezone) else display_tz
    return to_utc(dt).astimezone(zone)


def utc_epoch_seconds(dt: datetime) -> float:
    """Seconds since 1970-01-01T00:00:00Z for an aware ``dt``."""
    return (to_utc(dt) - _EPOCH).total_seconds()


def elapsed_seconds(start: datetime, end: datetime) -> float:
    """UTC elapsed seconds from ``start`` to ``end`` (negative if ``end`` is
    earlier). Both must be timezone-aware (Requirement 9.8)."""
    return (to_utc(end) - to_utc(start)).total_seconds()


def add_elapsed_seconds(start: datetime, seconds: float) -> datetime:
    """The UTC instant ``seconds`` of elapsed time after the aware ``start``."""
    return to_utc(start) + timedelta(seconds=seconds)
