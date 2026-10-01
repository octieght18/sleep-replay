"""The Data_Source_Adapter interface (Requirements 7.1, 7.2, 7.8, 17.4).

The adapter is the single seam between raw sources and the pipeline. Every
Importer (``backend.ingestion.fitbit``, ``backend.ingestion.sensorpush``) and
every future source implements :class:`Data_Source_Adapter` structurally; the
Aligner, Session_Detector, Feature_Extractor, Event_Detector, Sonification_Engine,
and Audio_Renderer only ever see the domain objects an adapter returns.

Operations
==========

``load(source, tz_context) -> LoadResult`` (required, Requirement 7.1)
    Inputs:
      * ``source`` -- a :data:`Source`: for the MVP Importers, one or more local
        file paths, or exactly one ``.zip`` archive path. Adapters decide how to
        read it; the pipeline treats it as opaque.
      * ``tz_context`` -- a :class:`TimezoneContext`: the Display_Timezone in
        effect when the import starts and the user's per-file-type
        Source_Timezone overrides (already validated by the registry). Call
        :meth:`TimezoneContext.source_timezone` to get the zone for a file type.

    Output: a :class:`LoadResult` with

      * ``telemetry`` -- TelemetryPoints (see below), values in *source* units;
      * ``sessions`` -- candidate SleepSessions found while loading (empty for
        adapters that have no sleep data);
      * ``session_hrv`` -- dated daily HRV summary rows (:class:`SessionHrvRow`)
        used to fill Session_HRV when a session has no in-session hrv_rmssd;
      * ``report`` -- the Import_Report (accepted / skipped / unsupported files,
        skipped-row and skipped-value counts, per-metric counts and coverage,
        applied Source_Timezones, warnings).

    A failure of the source as a whole (unreadable, unsupported, malformed)
    raises :class:`~backend.domain.errors.User_Error`, naming the affected file
    where applicable (Requirement 7.6). Recoverable per-file or per-row problems
    are recorded in the report instead.

``candidate_sessions(source, tz_context) -> list[SleepSession]`` (optional, 7.2)
    Same inputs as ``load``. Returns candidate SleepSessions, each with a
    timezone-aware ``start_time`` earlier than its ``end_time`` and
    Stage_Segments whose stages are :class:`~backend.domain.stages.Sleep_Stage`
    values. An adapter that doesn't define this method contributes zero
    candidates; call :func:`candidate_sessions_of` rather than the method
    directly so that rule is applied in one place. When an adapter implements
    both operations, ``load(...).sessions`` holds the same candidates, so the
    caller may use either without reading the source twice.

TelemetryPoint fields
=====================

Each :class:`~backend.domain.telemetry.TelemetryPoint` returned by ``load`` has:

* ``timestamp`` -- a timezone-aware ``datetime`` with an explicit UTC offset;
* ``source`` -- the adapter's Source_Identifier, optionally followed by
  :data:`SOURCE_SENSOR_SEPARATOR` and a sensor id (see :func:`compose_source`);
* ``metric`` -- one of the MVP :class:`~backend.domain.telemetry.Metric` values;
* ``value`` -- a finite number in the source unit, exactly as parsed;
* ``unit`` -- a source unit convertible to the metric's Canonical_Unit
  (``backend.domain.units.ALLOWED_UNITS``).

After ``load``, the registry's validation gate drops points with a naive
timestamp, a non-MVP metric, a non-finite value, or an inconvertible unit and
counts each reason in the Import_Report (Requirement 7.7).

MVP Metrics and Canonical_Units
===============================

==============  ==============  =====================================
Metric          Canonical_Unit  Accepted source units
==============  ==============  =====================================
``heart_rate``  ``bpm``         ``bpm``
``hrv_rmssd``   ``ms``          ``ms``
``steps``       ``steps/min``   ``steps/min``
``temperature`` ``°C``          ``°C``, ``°F``
``humidity``    ``percent``     ``percent``
``pressure``    ``hPa``         ``hPa``, ``mbar``, ``inHg``, ``kPa``
==============  ==============  =====================================

Values stay in source units until the Aligner converts them with
``backend.domain.units.to_canonical``.

Registering a new adapter
=========================

1. Create a sub-package ``backend/ingestion/<name>/``. It may import the
   standard library, third-party packages, and ``backend.domain`` only -- no
   other Importer sub-package and no processing, sonification, audio, or API
   package (Dependency_Rules, Requirement 17.2).
2. Write a class with a unique, stable ``source_identifier`` (for example
   ``"sensorpush_cloud"``), a ``load(source, tz_context)`` method returning a
   :class:`LoadResult`, and, if the source has sleep data, a
   ``candidate_sessions(source, tz_context)`` method. No base class is needed;
   the Protocol is structural. ``isinstance(obj, Data_Source_Adapter)`` checks
   the shape at runtime.
3. Register an instance with the adapter registry
   (``backend.ingestion.registry``) at startup, in ``backend.api.pipeline``.
   Imports for its Source_Identifier are then accepted through the Backend_API
   and its output flows to the Aligner and Session_Detector unchanged
   (Requirement 7.4).
4. Add tests under ``tests/`` using synthetic fixture files.

Extension point: SensorPush cloud API
-------------------------------------

A SensorPush cloud API adapter is a planned, *unimplemented* extension point.
It would follow the steps above with its own Source_Identifier and a
:data:`Source` describing the API query. The MVP needs no cloud credentials and
no network access (Requirement 7.5).

This module imports only the standard library and other ``backend.domain``
modules (Requirement 17.2a).
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from types import MappingProxyType
from typing import Protocol, TypeAlias, runtime_checkable
from zoneinfo import ZoneInfo

from backend.domain.errors import Import_Report
from backend.domain.session import SleepSession
from backend.domain.telemetry import TelemetryPoint
from backend.domain.timezones import resolve_source_timezone

__all__ = [
    "SourcePath",
    "Source",
    "source_paths",
    "is_zip_source",
    "SOURCE_SENSOR_SEPARATOR",
    "compose_source",
    "split_source",
    "TimezoneContext",
    "SessionHrvRow",
    "LoadResult",
    "Data_Source_Adapter",
    "SupportsCandidateSessions",
    "implements_candidate_sessions",
    "candidate_sessions_of",
]


# ---------------------------------------------------------------------------
# Source
# ---------------------------------------------------------------------------

#: One local file path.
SourcePath: TypeAlias = str | os.PathLike[str]

#: What an adapter reads. For the MVP Importers: a sequence of local file paths,
#: or exactly one ``.zip`` archive path (given alone or as a one-item sequence).
#: Whether a particular combination is acceptable (for example a zip mixed with
#: individual files) is the adapter's decision; the pipeline passes it through.
Source: TypeAlias = SourcePath | Sequence[SourcePath]


def source_paths(source: Source) -> tuple[Path, ...]:
    """Return the paths in ``source`` as a tuple of :class:`~pathlib.Path`, in order.

    A single path (``str`` or path-like) gives a one-item tuple. Nothing is read
    from disk.
    """
    if isinstance(source, (str, os.PathLike)):
        return (Path(source),)
    return tuple(Path(p) for p in source)


def is_zip_source(source: Source) -> bool:
    """True iff ``source`` is exactly one path with a ``.zip`` suffix (case-insensitive)."""
    paths = source_paths(source)
    return len(paths) == 1 and paths[0].suffix.lower() == ".zip"


# ---------------------------------------------------------------------------
# TelemetryPoint.source composition
# ---------------------------------------------------------------------------

#: Separates the Source_Identifier from a sensor id in ``TelemetryPoint.source``.
SOURCE_SENSOR_SEPARATOR = ":"


def compose_source(source_identifier: str, sensor_id: str | None = None) -> str:
    """Build a ``TelemetryPoint.source`` value.

    ``compose_source("sensorpush", "A1")`` is ``"sensorpush:A1"``. An empty or
    whitespace-only ``sensor_id`` (or ``None``) gives the bare identifier
    (Requirement 6.11). The sensor id is stripped of surrounding whitespace.

    Raises:
        ValueError: ``source_identifier`` is empty or contains the separator.
    """
    if not source_identifier or SOURCE_SENSOR_SEPARATOR in source_identifier:
        raise ValueError(f"invalid Source_Identifier: {source_identifier!r}")
    sensor = (sensor_id or "").strip()
    return f"{source_identifier}{SOURCE_SENSOR_SEPARATOR}{sensor}" if sensor else source_identifier


def split_source(source: str) -> tuple[str, str | None]:
    """Inverse of :func:`compose_source`: ``(source_identifier, sensor_id or None)``."""
    identifier, sep, sensor = source.partition(SOURCE_SENSOR_SEPARATOR)
    return (identifier, sensor) if sep and sensor else (identifier, None)


# ---------------------------------------------------------------------------
# Timezone context
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TimezoneContext:
    """Timezone inputs to an adapter operation (Requirement 9.1, 9.2).

    Attributes:
        display_tz: The Display_Timezone in effect when the import starts.
        overrides: File type (``backend.domain.timezones.FILE_TYPES``) -> IANA
            name of the user's Source_Timezone override. Stored read-only.
    """

    display_tz: ZoneInfo
    overrides: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "overrides", MappingProxyType(dict(self.overrides)))

    def source_timezone(self, file_type: str) -> ZoneInfo:
        """The Source_Timezone for ``file_type``: override, else the documented default.

        Raises:
            ValueError: unknown ``file_type``.
            User_Error: ``INVALID_TIMEZONE`` for an invalid override.
        """
        return resolve_source_timezone(file_type, self.overrides, self.display_tz)


# ---------------------------------------------------------------------------
# Load result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SessionHrvRow:
    """One daily HRV summary row: the rmssd value (ms) for a calendar date.

    Attributes:
        date: The row's calendar date, as written in the source.
        rmssd: Daily rmssd in ms.
        source_file: Originating file name, when known.
    """

    date: date
    rmssd: float
    source_file: str | None = None


@dataclass
class LoadResult:
    """Everything one ``load`` call produced.

    Attributes:
        telemetry: TelemetryPoints in source units.
        sessions: Candidate SleepSessions (empty when the adapter has no sleep data).
        session_hrv: Daily HRV summary rows for Session_HRV (Requirement 4.3).
        report: The Import_Report for this load.
        file_dates: Optional. File name (as reported in the Import_Report) ->
            the date written in that file's name, for adapters whose files are
            dated by name (the Fitbit_Importer uses it for the selection
            window, Requirement 4.5). Empty for adapters without dated files.
    """

    telemetry: list[TelemetryPoint] = field(default_factory=list)
    sessions: list[SleepSession] = field(default_factory=list)
    session_hrv: list[SessionHrvRow] = field(default_factory=list)
    report: Import_Report = field(default_factory=Import_Report)
    file_dates: dict[str, date] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Adapter protocols
# ---------------------------------------------------------------------------


@runtime_checkable
class Data_Source_Adapter(Protocol):  # noqa: N801 - name follows the spec glossary
    """A data source the pipeline can import from (Requirement 7.1).

    See the module docstring for the full contract. ``candidate_sessions`` is
    optional and therefore described by :class:`SupportsCandidateSessions`.
    """

    #: Stable, unique Source_Identifier, e.g. ``"fitbit"`` or ``"sensorpush"``.
    source_identifier: str

    def load(self, source: Source, tz_context: TimezoneContext) -> LoadResult:
        """Read ``source`` and return its TelemetryPoints, candidate sessions,
        daily HRV rows, and Import_Report.

        Every TelemetryPoint has a timezone-aware timestamp, ``source`` equal to
        ``source_identifier`` (optionally plus a sensor id), an MVP Metric, a
        finite value, and a unit convertible to the Metric's Canonical_Unit.

        Raises:
            User_Error: the source as a whole can't be loaded (Requirement 7.6).
        """
        ...


@runtime_checkable
class SupportsCandidateSessions(Protocol):
    """The optional candidate-session operation (Requirement 7.2)."""

    def candidate_sessions(self, source: Source, tz_context: TimezoneContext) -> list[SleepSession]:
        """Return candidate SleepSessions, each with a timezone-aware
        ``start_time < end_time`` and Stage_Segments with Sleep_Stage stages."""
        ...


def implements_candidate_sessions(adapter: object) -> bool:
    """True iff ``adapter`` has a callable ``candidate_sessions`` method."""
    return callable(getattr(adapter, "candidate_sessions", None))


def candidate_sessions_of(
    adapter: Data_Source_Adapter, source: Source, tz_context: TimezoneContext
) -> list[SleepSession]:
    """Candidate SleepSessions from ``adapter``; ``[]`` when it doesn't implement
    ``candidate_sessions`` (Requirement 7.2)."""
    if not implements_candidate_sessions(adapter):
        return []
    return list(adapter.candidate_sessions(source, tz_context))  # type: ignore[attr-defined]
