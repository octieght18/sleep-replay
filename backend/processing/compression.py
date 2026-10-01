"""Night compression: Target_Duration, Compression_Ratio, Night_Time <-> Replay_Time (Requirement 11).

Target_Duration
---------------
Exactly five Target_Durations are offered: 30, 120, 180, 300, and 600 seconds
(:data:`ALLOWED_TARGET_DURATIONS`, Requirement 11.1). :func:`select_target_duration`
picks, in order, the value in the generation request, then the active
Mapping_Config's ``target_duration``, then 180 s (Requirement 11.2). Any other
value raises a ``INVALID_TARGET_DURATION`` :class:`User_Error` listing the five
accepted values (Requirement 11.7). Nothing is stored here, so a rejected value
leaves the previously active Target_Duration untouched.

Compression_Ratio and time mapping
----------------------------------
:class:`Night_Compression` holds one SleepSession's mapping. The session
duration is the UTC elapsed time from start_time to end_time (Requirement 9.8),
kept as an exact integer count of microseconds. From it:

* ``ratio = duration / Target_Duration`` (full float precision internally;
  :meth:`Night_Compression.ratio_rounded` / :meth:`Night_Compression.ratio_text`
  give the value for the Replay_Manifest with at least 3 decimals, Req 11.3).
  An 8-hour session at 180 s gives exactly 160.0.
* ``replay_time(t) = (t - start_time) / ratio`` is evaluated as
  ``offset_us * Target_Duration / duration_us`` with exact integer operands and
  one correctly rounded division. So start_time maps to exactly ``0.0``,
  end_time to exactly ``float(Target_Duration)``, and every value is within
  one float rounding (far below 1 ms) of the formula (Requirement 11.4).
* Order preservation (Requirement 11.5): Python datetimes have microsecond
  resolution, so distinct instants differ by at least 1 us. The numerators of
  two such instants differ by at least ``Target_Duration``, and the quotients
  differ by ``Target_Duration / duration_us``, which exceeds the float spacing
  near ``Target_Duration`` for any session shorter than about 140 years.
  Correctly rounded division is monotone, so ``t1 < t2`` gives
  ``replay_time(t1) < replay_time(t2)``. Instants closer than 1 us cannot be
  represented and so cannot be ordered.
* :meth:`Night_Compression.night_time` is the inverse, rounding to the nearest
  microsecond; ``night_time(replay_time(t)) == t`` for every in-session ``t``.

A session whose duration is less than or equal to the Target_Duration raises a
``SESSION_TOO_SHORT`` :class:`User_Error` listing the offered Target_Durations
shorter than the session, or stating that the session is too short to replay
when none is (Requirement 11.6).

Only the standard library, ``backend.domain``, and this package are imported.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from fractions import Fraction
from numbers import Real
from typing import Any, Final, Protocol

from backend.domain.errors import INVALID_TARGET_DURATION, SESSION_TOO_SHORT, User_Error
from backend.processing.timezones import to_utc

__all__ = [
    "ALLOWED_TARGET_DURATIONS",
    "DEFAULT_TARGET_DURATION",
    "RATIO_DECIMALS",
    "Night_Compression",
    "night_compression",
    "validate_target_duration",
    "select_target_duration",
    "session_duration_microseconds",
    "session_duration_seconds",
    "offered_durations_shorter_than",
    "compression_ratio",
    "replay_time",
    "night_time",
    "format_target_duration",
]

#: The five offered Target_Durations, in seconds, ascending (Requirement 11.1).
ALLOWED_TARGET_DURATIONS: Final[tuple[int, ...]] = (30, 120, 180, 300, 600)

#: Target_Duration used when neither the request nor the Mapping_Config sets one (Req 11.2).
DEFAULT_TARGET_DURATION: Final[int] = 180

#: Minimum decimal places for the Compression_Ratio in the Replay_Manifest (Req 11.3).
RATIO_DECIMALS: Final[int] = 3

_US_PER_S: Final[int] = 1_000_000
_ONE_US: Final[timedelta] = timedelta(microseconds=1)

_DURATION_LABELS: Final[Mapping[int, str]] = {
    30: "30 seconds",
    120: "2 minutes",
    180: "3 minutes",
    300: "5 minutes",
    600: "10 minutes",
}


class _HasSessionBounds(Protocol):
    start_time: datetime
    end_time: datetime


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def format_target_duration(seconds: int) -> str:
    """Human label for an offered Target_Duration, e.g. ``180 -> "3 minutes"``."""
    return _DURATION_LABELS.get(seconds, f"{seconds} seconds")


def _join_labels(seconds: tuple[int, ...]) -> str:
    labels = [f"{format_target_duration(s)} ({s} s)" for s in seconds]
    if len(labels) <= 1:
        return "".join(labels)
    return ", ".join(labels[:-1]) + " or " + labels[-1]


def _format_elapsed(duration_us: int) -> str:
    """Plain-language session length, e.g. ``"2 min 10 s"`` or ``"45.5 s"``."""
    if duration_us <= 0:
        return "0 s"
    total_s = duration_us / _US_PER_S
    if total_s < 60:
        return f"{total_s:g} s"
    whole, frac_us = divmod(duration_us, _US_PER_S)
    hours, rem = divmod(whole, 3600)
    minutes, secs = divmod(rem, 60)
    parts = []
    if hours:
        parts.append(f"{hours} h")
    if minutes:
        parts.append(f"{minutes} min")
    if secs or frac_us:
        # Keep fractional seconds so e.g. 180.5 s never reads as "3 min".
        parts.append(f"{(secs * _US_PER_S + frac_us) / _US_PER_S:g} s")
    return " ".join(parts)


def _describe_value(value: Any) -> str:
    if isinstance(value, str):
        return repr(value)
    return str(value)


# ---------------------------------------------------------------------------
# Target_Duration validation and selection
# ---------------------------------------------------------------------------


def _invalid_target(value: Any) -> User_Error:
    accepted = ", ".join(str(s) for s in ALLOWED_TARGET_DURATIONS)
    return User_Error(
        INVALID_TARGET_DURATION,
        (
            f"The Target_Duration {_describe_value(value)} is not supported. The accepted values are "
            f"{_join_labels(ALLOWED_TARGET_DURATIONS)}."
        ),
        f"Choose one of the accepted Target_Durations in seconds: {accepted}.",
        details={"value": _describe_value(value), "accepted_values": accepted},
    )


def validate_target_duration(value: Any) -> int:
    """Return ``value`` as one of :data:`ALLOWED_TARGET_DURATIONS` (Requirement 11.1, 11.7).

    Accepts an ``int`` or an integral finite real (e.g. ``180.0``). Booleans,
    strings, non-integral or non-finite numbers, and any other value raise.

    Raises:
        User_Error: ``INVALID_TARGET_DURATION``, listing the five accepted values.
    """
    if isinstance(value, bool) or not isinstance(value, Real):
        raise _invalid_target(value)
    if isinstance(value, int):
        seconds = value
    else:
        as_float = float(value)
        if not math.isfinite(as_float) or not as_float.is_integer():
            raise _invalid_target(value)
        seconds = int(as_float)
    if seconds not in ALLOWED_TARGET_DURATIONS:
        raise _invalid_target(value)
    return seconds


def _config_target(config: Any) -> Any:
    """The ``target_duration`` of a Mapping_Config-like value, or ``None`` when unset."""
    if config is None:
        return None
    if isinstance(config, Mapping):
        return config.get("target_duration")
    return getattr(config, "target_duration", None)


def select_target_duration(request: Any = None, config: Any = None) -> int:
    """Select the Target_Duration in seconds (Requirement 11.2, 11.7).

    Precedence: ``request`` when not ``None``, then the Mapping_Config's
    ``target_duration`` when set, then :data:`DEFAULT_TARGET_DURATION` (180 s).

    Args:
        request: Target_Duration from the generation request, or ``None``.
        config: The active Mapping_Config: an object with a ``target_duration``
            attribute, a mapping with a ``"target_duration"`` key, or ``None``.

    Raises:
        User_Error: ``INVALID_TARGET_DURATION`` when the value that takes
            precedence is not one of the five accepted values. An invalid
            value never falls through to a lower-precedence source.
    """
    if request is not None:
        return validate_target_duration(request)
    configured = _config_target(config)
    if configured is not None:
        return validate_target_duration(configured)
    return DEFAULT_TARGET_DURATION


# ---------------------------------------------------------------------------
# Session duration
# ---------------------------------------------------------------------------


def _elapsed_microseconds(start: datetime, end: datetime) -> int:
    """Exact UTC elapsed microseconds from ``start`` to ``end`` (both aware)."""
    return (to_utc(end) - to_utc(start)) // _ONE_US


def session_duration_microseconds(session: _HasSessionBounds) -> int:
    """UTC elapsed time from start_time to end_time, in whole microseconds (Req 9.8)."""
    return _elapsed_microseconds(session.start_time, session.end_time)


def session_duration_seconds(session: _HasSessionBounds) -> float:
    """UTC elapsed time from start_time to end_time, in seconds (Req 9.8, 11.3)."""
    return session_duration_microseconds(session) / _US_PER_S


def offered_durations_shorter_than(duration_s: float) -> tuple[int, ...]:
    """Offered Target_Durations strictly shorter than ``duration_s``, ascending."""
    return tuple(s for s in ALLOWED_TARGET_DURATIONS if s < duration_s)


def _session_too_short(duration_us: int, target_s: int) -> User_Error:
    shorter = tuple(s for s in ALLOWED_TARGET_DURATIONS if s * _US_PER_S < duration_us)
    length = _format_elapsed(duration_us)
    details = {
        "session_duration_s": f"{duration_us / _US_PER_S:g}",
        "target_duration_s": str(target_s),
        "shorter_durations": ", ".join(str(s) for s in shorter),
    }
    if shorter:
        return User_Error(
            SESSION_TOO_SHORT,
            (
                f"The sleep session lasts {length}, which is not longer than the selected "
                f"Target_Duration of {format_target_duration(target_s)}. Target_Durations shorter "
                f"than the session: {_join_labels(shorter)}."
            ),
            f"Choose a shorter Target_Duration: {_join_labels(shorter)}.",
            details=details,
        )
    return User_Error(
        SESSION_TOO_SHORT,
        (
            f"The sleep session lasts {length}, which is too short to replay: it is not longer "
            f"than any offered Target_Duration."
        ),
        (
            f"Select or enter a sleep session longer than "
            f"{format_target_duration(ALLOWED_TARGET_DURATIONS[0])}."
        ),
        details=details,
    )


# ---------------------------------------------------------------------------
# Night_Compression
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Night_Compression:  # noqa: N801 - name follows the spec glossary
    """The Night_Time <-> Replay_Time mapping of one SleepSession (Requirement 11.3-11.5).

    Construction converts ``start_time`` / ``end_time`` to UTC and validates
    ``target_duration_s``. ``ratio`` (the Compression_Ratio) is derived.

    Attributes:
        start_time: Session start as a UTC instant.
        end_time: Session end as a UTC instant.
        target_duration_s: The Target_Duration in seconds.
        ratio: Session duration / Target_Duration, full float precision.

    Raises:
        User_Error: ``INVALID_TARGET_DURATION`` for a disallowed Target_Duration;
            ``SESSION_TOO_SHORT`` when the session duration is less than or
            equal to the Target_Duration (Requirement 11.6).
        ValueError: ``start_time`` or ``end_time`` is offset-free.
    """

    start_time: datetime
    end_time: datetime
    target_duration_s: int
    ratio: float = field(init=False)
    _duration_us: int = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        target = validate_target_duration(self.target_duration_s)
        start = to_utc(self.start_time)
        end = to_utc(self.end_time)
        duration_us = (end - start) // _ONE_US
        if duration_us <= target * _US_PER_S:
            raise _session_too_short(duration_us, target)
        object.__setattr__(self, "start_time", start)
        object.__setattr__(self, "end_time", end)
        object.__setattr__(self, "target_duration_s", target)
        object.__setattr__(self, "_duration_us", duration_us)
        # int / int true division is correctly rounded: 8 h / 180 s -> 160.0 exactly.
        object.__setattr__(self, "ratio", duration_us / (target * _US_PER_S))

    @classmethod
    def for_session(cls, session: _HasSessionBounds, target_duration_s: int) -> Night_Compression:
        """Build the mapping for ``session`` (anything with start_time/end_time)."""
        return cls(session.start_time, session.end_time, target_duration_s)

    # -- derived values -----------------------------------------------------

    @property
    def duration_microseconds(self) -> int:
        """Session duration in whole microseconds (UTC elapsed)."""
        return self._duration_us

    @property
    def duration_seconds(self) -> float:
        """Session duration in seconds (UTC elapsed)."""
        return self._duration_us / _US_PER_S

    @property
    def ratio_exact(self) -> Fraction:
        """The Compression_Ratio as an exact fraction."""
        return Fraction(self._duration_us, self.target_duration_s * _US_PER_S)

    def ratio_rounded(self, decimals: int = RATIO_DECIMALS) -> float:
        """Compression_Ratio rounded for the Replay_Manifest (Req 11.3; at least 3 decimals)."""
        if decimals < RATIO_DECIMALS:
            raise ValueError(f"the Compression_Ratio needs at least {RATIO_DECIMALS} decimal places")
        return round(self.ratio, decimals)

    def ratio_text(self, decimals: int = RATIO_DECIMALS) -> str:
        """Compression_Ratio as fixed-point text, e.g. ``"160.000"`` (Req 11.3)."""
        if decimals < RATIO_DECIMALS:
            raise ValueError(f"the Compression_Ratio needs at least {RATIO_DECIMALS} decimal places")
        return f"{self.ratio:.{decimals}f}"

    # -- mapping ------------------------------------------------------------

    def replay_time(self, t: datetime) -> float:
        """Replay_Time in seconds for the Night_Time ``t`` (Requirement 11.4, 11.5).

        ``start_time`` gives exactly ``0.0`` and ``end_time`` exactly
        ``float(target_duration_s)``; the mapping is strictly increasing at
        microsecond resolution.

        Raises:
            ValueError: ``t`` is offset-free or outside ``[start_time, end_time]``.
        """
        offset_us = (to_utc(t) - self.start_time) // _ONE_US
        if offset_us < 0 or offset_us > self._duration_us:
            raise ValueError(
                f"Night_Time {t.isoformat()} is outside the session "
                f"[{self.start_time.isoformat()}, {self.end_time.isoformat()}]"
            )
        return (offset_us * self.target_duration_s) / self._duration_us

    def night_time(self, replay_s: float) -> datetime:
        """Night_Time (UTC, nearest microsecond) for a Replay_Time in seconds.

        Inverse of :meth:`replay_time`: ``night_time(0)`` is ``start_time``,
        ``night_time(target_duration_s)`` is ``end_time``, and
        ``night_time(replay_time(t)) == t`` for every in-session ``t``.

        Raises:
            ValueError: ``replay_s`` is not finite or outside ``[0, target_duration_s]``.
        """
        value = float(replay_s)
        if not math.isfinite(value) or value < 0 or value > self.target_duration_s:
            raise ValueError(f"Replay_Time {replay_s!r} is outside [0, {self.target_duration_s}]")
        offset_us = round(Fraction(value) * self._duration_us / self.target_duration_s)
        return self.start_time + timedelta(microseconds=offset_us)


# ---------------------------------------------------------------------------
# Module-level conveniences
# ---------------------------------------------------------------------------


def night_compression(session: _HasSessionBounds, target_duration_s: int) -> Night_Compression:
    """Build the :class:`Night_Compression` for ``session`` and a Target_Duration."""
    return Night_Compression.for_session(session, target_duration_s)


def compression_ratio(session: _HasSessionBounds, target_duration_s: int) -> float:
    """Compression_Ratio = UTC session duration (s) / Target_Duration (s) (Requirement 11.3).

    Raises:
        User_Error: ``INVALID_TARGET_DURATION`` or ``SESSION_TOO_SHORT`` (Req 11.6, 11.7).
    """
    return Night_Compression.for_session(session, target_duration_s).ratio


def replay_time(mapping: Night_Compression, t: datetime) -> float:
    """Replay_Time in seconds of Night_Time ``t`` under ``mapping`` (Req 11.4, 11.5)."""
    return mapping.replay_time(t)


def night_time(mapping: Night_Compression, replay_s: float) -> datetime:
    """Night_Time (UTC) of Replay_Time ``replay_s`` under ``mapping``."""
    return mapping.night_time(replay_s)
