"""The Fitbit_Importer: a ``Data_Source_Adapter`` with Source_Identifier ``fitbit``.

Combines the sibling modules into the two adapter operations:

* :mod:`.packaging` -- individual files or one zip, file-name matching,
  unsupported files, and per-member skip reasons (Requirement 5);
* :mod:`.sleep` -- candidate SleepSessions from ``sleep-*.json`` (Requirement 3);
* :mod:`.intraday` -- heart_rate and steps TelemetryPoints (4.1, 4.4);
* :mod:`.hrv` -- hrv_rmssd TelemetryPoints and daily summary rows (4.2, 4.3).

``FitbitImporter.load(source, tz_context)`` returns a
:class:`~backend.domain.adapter.LoadResult` whose Import_Report holds:

* ``accepted_files`` -- every matched file that was read and parsed (files
  skipped as a whole are in ``skipped_files`` with their reason instead);
* ``per_metric_counts`` / ``per_metric_coverage`` -- per Metric, the number of
  points and the ``(first, last)`` instants in UTC. A metric whose files were
  loaded but yielded no point is recorded with count 0 and coverage ``None``;
* ``applied_source_timezones`` -- file type (``backend.domain.timezones``
  ``FITBIT_*``) -> IANA name of the Source_Timezone applied to it, for every
  file type that was read (Requirement 9.1);
* one DST warning per file with adjusted timestamps (Requirement 9.4).

``LoadResult.file_dates`` maps each matched file name with a valid date in its
name to that date (all file types, including files not loaded because of a
selection window; a month-only daily HRV summary maps to the 1st of the month).
``LoadResult.session_hrv`` holds every daily HRV summary row.

Selection window (Requirement 4.5)
==================================

:func:`files_in_selection_window` selects the names whose file-name date lies
from one calendar day before the start date through one calendar day after the
end date, both dates taken in the Display_Timezone. ``load`` applies it when
called with ``selection=<SleepSession>``: heart rate, steps, and HRV details
files outside the window are then not read at all (sleep files and daily HRV
summaries are always read). The same call adds the per-session missing-metric
warnings (Requirement 4.6, see :func:`missing_metric_warnings`), which the
pipeline can also add on its own for a session selected after the import.

Candidate sessions
==================

``candidate_sessions(source, tz_context)`` returns the same SleepSessions as
``load(source, tz_context).sessions``. The importer keeps the sessions of its
most recent ``load``/``candidate_sessions`` call, keyed on the resolved source
paths with their size and modification time plus the timezone context, so the
registry's ``load`` followed by ``candidate_sessions`` reads the source once.
On a cache miss only the sleep files are read. Callers that already have a
``LoadResult`` may use ``load(...).sessions`` directly.

This module imports only the standard library, ``backend.domain``, and sibling
modules of this sub-package (Dependency_Rules, Requirement 17.2).
"""

from __future__ import annotations

import threading
from collections.abc import Hashable, Iterable, Mapping
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from backend.domain.adapter import LoadResult, SessionHrvRow, Source, TimezoneContext, source_paths
from backend.domain.errors import Import_Report, Warning_Item
from backend.domain.session import SleepSession
from backend.domain.telemetry import Metric, TelemetryPoint
from backend.domain.timezones import (
    FITBIT_HEART_RATE,
    FITBIT_HRV_DETAILS,
    FITBIT_HRV_SUMMARY,
    FITBIT_SLEEP,
    FITBIT_STEPS,
    DstAdjustmentCounter,
)

from .hrv import parse_hrv_details, parse_hrv_summary, session_hrv_for
from .intraday import FITBIT_SOURCE_IDENTIFIER, parse_intraday
from .packaging import FitbitExport, open_fitbit_export
from .sleep import parse_sleep_json

__all__ = [
    "SOURCE_IDENTIFIER",
    "FITBIT_FILE_TYPES",
    "WINDOWED_FILE_TYPES",
    "SESSION_METRICS",
    "selection_window",
    "files_in_selection_window",
    "missing_metric_warnings",
    "add_missing_metric_warnings",
    "FitbitImporter",
]

#: Source_Identifier of the Fitbit_Importer.
SOURCE_IDENTIFIER: str = FITBIT_SOURCE_IDENTIFIER

#: Every Fitbit file type, in documentation order.
FITBIT_FILE_TYPES: tuple[str, ...] = (
    FITBIT_SLEEP,
    FITBIT_HEART_RATE,
    FITBIT_STEPS,
    FITBIT_HRV_DETAILS,
    FITBIT_HRV_SUMMARY,
)

#: File types restricted to the selection window (Requirement 4.5).
WINDOWED_FILE_TYPES: frozenset[str] = frozenset({FITBIT_HEART_RATE, FITBIT_STEPS, FITBIT_HRV_DETAILS})

#: Metrics checked per selected session (Requirement 4.6), in warning order.
SESSION_METRICS: tuple[Metric, ...] = (Metric.heart_rate, Metric.hrv_rmssd, Metric.steps)

# Metric produced by each telemetry file type.
_FILE_TYPE_METRIC: Mapping[str, Metric] = {
    FITBIT_HEART_RATE: Metric.heart_rate,
    FITBIT_STEPS: Metric.steps,
    FITBIT_HRV_DETAILS: Metric.hrv_rmssd,
}

_UTC = timezone.utc
_ONE_DAY = timedelta(days=1)


# ---------------------------------------------------------------------------
# Selection window (Requirement 4.5)
# ---------------------------------------------------------------------------


def _require_aware(value: datetime, name: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be a timezone-aware datetime")


def selection_window(start: datetime, end: datetime, display_tz: ZoneInfo) -> tuple[date, date]:
    """The inclusive file-name date range for a session from ``start`` to ``end``.

    Returns ``(start date - 1 day, end date + 1 day)`` with both dates taken in
    ``display_tz`` (clamped to ``date.min`` / ``date.max``).

    Raises:
        ValueError: ``start`` or ``end`` is not timezone-aware.
    """
    _require_aware(start, "start")
    _require_aware(end, "end")
    first = start.astimezone(display_tz).date()
    last = end.astimezone(display_tz).date()
    first = first - _ONE_DAY if first > date.min else first
    last = last + _ONE_DAY if last < date.max else last
    return first, last


def files_in_selection_window(
    file_dates: Mapping[str, date | None],
    start: datetime,
    end: datetime,
    display_tz: ZoneInfo,
) -> list[str]:
    """Names in ``file_dates`` whose date lies in :func:`selection_window`.

    Names mapped to ``None`` (no valid date in the file name) are never
    selected. Order follows ``file_dates``.

    Raises:
        ValueError: ``start`` or ``end`` is not timezone-aware.
    """
    first, last = selection_window(start, end, display_tz)
    return [name for name, day in file_dates.items() if day is not None and first <= day <= last]


# ---------------------------------------------------------------------------
# Missing-metric warnings (Requirement 4.6)
# ---------------------------------------------------------------------------

_METRIC_FILES: Mapping[Metric, str] = {
    Metric.heart_rate: "heart_rate-YYYY-MM-DD.json",
    Metric.steps: "steps-YYYY-MM-DD.json",
    Metric.hrv_rmssd: "Heart Rate Variability Details - YYYY-MM-DD.csv",
}


def _format_instant(value: datetime, display_tz: ZoneInfo | None) -> str:
    zone = display_tz or _UTC
    return value.astimezone(zone).strftime("%Y-%m-%d %H:%M %Z").strip()


def _missing_metric_warning(metric: Metric, session: SleepSession, display_tz: ZoneInfo | None) -> Warning_Item:
    span = f"{_format_instant(session.start_time, display_tz)} to {_format_instant(session.end_time, display_tz)}"
    if metric is Metric.hrv_rmssd:
        description = (
            f"No hrv_rmssd data lies within the selected sleep session ({span}), and no daily "
            "heart rate variability summary value is available for it. The replay continues without HRV."
        )
        action = (
            f"If this night should have HRV data, import the '{_METRIC_FILES[metric]}' or "
            "'Daily Heart Rate Variability Summary - YYYY-MM(-DD).csv' files covering it."
        )
    else:
        description = (
            f"No {metric.value} data lies within the selected sleep session ({span}). "
            f"The replay continues without {metric.value}."
        )
        action = (
            f"If this night should have {metric.value} data, import the '{_METRIC_FILES[metric]}' files "
            f"covering it, or import them again with a different Source_Timezone for {metric.value} files."
        )
    return Warning_Item(description, subject=metric.value, recommended_action=action)


def missing_metric_warnings(
    session: SleepSession,
    telemetry: Iterable[TelemetryPoint],
    session_hrv_rows: Iterable[SessionHrvRow] = (),
    display_tz: ZoneInfo | None = None,
) -> list[Warning_Item]:
    """One warning per metric of :data:`SESSION_METRICS` absent from ``session``.

    A metric is absent when no point of ``telemetry`` with that metric lies in
    ``[session.start_time, session.end_time]`` (compared as UTC instants). For
    hrv_rmssd it must also have no Session_HRV: ``session.session_hrv`` is
    ``None`` and, when ``display_tz`` is given, :func:`~.hrv.session_hrv_for`
    finds no matching row in ``session_hrv_rows`` (Requirement 4.3, 4.6).
    Times in the text are shown in ``display_tz`` (UTC when not given).
    """
    start = session.start_time.astimezone(_UTC)
    end = session.end_time.astimezone(_UTC)
    present: set[Metric] = set()
    for point in telemetry:
        if point.metric in SESSION_METRICS and point.metric not in present:
            if start <= point.timestamp.astimezone(_UTC) <= end:
                present.add(point.metric)
    if Metric.hrv_rmssd not in present:
        if session.session_hrv is not None:
            present.add(Metric.hrv_rmssd)
        elif display_tz is not None and session_hrv_for(session, session_hrv_rows, display_tz) is not None:
            present.add(Metric.hrv_rmssd)
    return [_missing_metric_warning(m, session, display_tz) for m in SESSION_METRICS if m not in present]


def add_missing_metric_warnings(
    report: Import_Report,
    session: SleepSession,
    telemetry: Iterable[TelemetryPoint],
    session_hrv_rows: Iterable[SessionHrvRow] = (),
    display_tz: ZoneInfo | None = None,
) -> list[Warning_Item]:
    """Add :func:`missing_metric_warnings` to ``report``, skipping any already there.

    Calling it again for the same session and report adds nothing, so each
    metric is warned about once per session. Returns the warnings added.
    """
    added: list[Warning_Item] = []
    for item in missing_metric_warnings(session, telemetry, session_hrv_rows, display_tz):
        if item not in report.warnings:
            report.warnings.append(item)
            added.append(item)
    return added


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def _resolve_zones(tz_context: TimezoneContext) -> dict[str, ZoneInfo]:
    """Source_Timezone per Fitbit file type; validates every Fitbit override up front."""
    return {file_type: tz_context.source_timezone(file_type) for file_type in FITBIT_FILE_TYPES}


def _zone_name(zone: ZoneInfo) -> str:
    return zone.key or str(zone)


def _record_metrics(report: Import_Report, telemetry: list[TelemetryPoint], loaded_metrics: set[Metric]) -> None:
    counts: dict[Metric, int] = {}
    bounds: dict[Metric, tuple[datetime, datetime]] = {}
    for point in telemetry:
        ts = point.timestamp.astimezone(_UTC)
        counts[point.metric] = counts.get(point.metric, 0) + 1
        if point.metric in bounds:
            first, last = bounds[point.metric]
            bounds[point.metric] = (min(first, ts), max(last, ts))
        else:
            bounds[point.metric] = (ts, ts)
    for metric in SESSION_METRICS:
        if metric in counts:
            report.record_metric(metric.value, counts[metric], bounds[metric])
        elif metric in loaded_metrics:
            report.record_metric(metric.value, 0, None)


def _load_export(
    export: FitbitExport,
    zones: Mapping[str, ZoneInfo],
    display_tz: ZoneInfo,
    report: Import_Report,
    counter: DstAdjustmentCounter,
    selection: SleepSession | None,
    sleep_only: bool = False,
) -> LoadResult:
    file_dates = {ref.name: ref.file_date for ref in export.files if ref.file_date is not None}

    windowed: set[str] | None = None
    if selection is not None:
        dated = {ref.name: ref.file_date for ref in export.files if ref.file_type in WINDOWED_FILE_TYPES}
        windowed = set(files_in_selection_window(dated, selection.start_time, selection.end_time, display_tz))

    telemetry: list[TelemetryPoint] = []
    sessions: list[SleepSession] = []
    hrv_rows: list[SessionHrvRow] = []
    loaded_metrics: set[Metric] = set()

    for ref in export.files:
        file_type = ref.file_type
        if sleep_only and file_type != FITBIT_SLEEP:
            continue
        if windowed is not None and file_type in WINDOWED_FILE_TYPES and ref.name not in windowed:
            continue
        data = export.read(ref)  # None: skip reason already recorded
        if data is None:
            continue
        zone = zones[file_type]
        report.applied_source_timezones[file_type] = _zone_name(zone)

        if file_type == FITBIT_SLEEP:
            parsed = parse_sleep_json(data, ref.name, zone, report, counter)
            if parsed is None:
                continue
            sessions.extend(parsed)
        elif file_type == FITBIT_HRV_SUMMARY:
            summary = parse_hrv_summary(data, ref.name, report)
            if summary.skipped:
                continue
            hrv_rows.extend(summary.rows)
        else:
            if file_type == FITBIT_HRV_DETAILS:
                result = parse_hrv_details(data, ref.name, zone, counter, report)
            else:
                result = parse_intraday(file_type, data, ref.name, zone, counter, report)
            if result.skipped:
                continue
            telemetry.extend(result.points)
            loaded_metrics.add(_FILE_TYPE_METRIC[file_type])
        report.accepted_files.append(ref.name)

    _record_metrics(report, telemetry, loaded_metrics)
    return LoadResult(
        telemetry=telemetry,
        sessions=sessions,
        session_hrv=hrv_rows,
        report=report,
        file_dates=file_dates,
    )


def _cache_key(source: Source, tz_context: TimezoneContext) -> Hashable | None:
    """Identity of a load: resolved paths with size and mtime, plus the timezones.

    ``None`` (never cached) when a path can't be resolved or stat'ed.
    """
    try:
        files = []
        for path in source_paths(source):
            stat = path.stat()
            files.append((str(path.resolve()), stat.st_size, stat.st_mtime_ns))
    except (OSError, TypeError, ValueError):
        return None
    overrides = tuple(sorted((str(k), str(v)) for k, v in tz_context.overrides.items()))
    return (tuple(files), _zone_name(tz_context.display_tz), overrides)


class FitbitImporter:
    """``Data_Source_Adapter`` for a Fitbit_Export (``load`` and ``candidate_sessions``).

    ``source`` is one or more local Fitbit files, or exactly one ``.zip``
    archive. Source_Timezones come from ``tz_context.source_timezone(file_type)``
    for the ``FITBIT_*`` file types. See the module docstring for what the
    result and its Import_Report contain.
    """

    source_identifier: str = SOURCE_IDENTIFIER

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cached_key: Hashable | None = None
        self._cached_sessions: tuple[SleepSession, ...] = ()

    # -- cache ----------------------------------------------------------------

    def _remember(self, key: Hashable | None, sessions: Iterable[SleepSession]) -> None:
        with self._lock:
            self._cached_key = key
            self._cached_sessions = tuple(sessions) if key is not None else ()

    def _cached(self, key: Hashable | None) -> list[SleepSession] | None:
        if key is None:
            return None
        with self._lock:
            return list(self._cached_sessions) if key == self._cached_key else None

    # -- adapter operations -----------------------------------------------------

    def load(
        self,
        source: Source,
        tz_context: TimezoneContext,
        *,
        selection: SleepSession | None = None,
    ) -> LoadResult:
        """Read the Fitbit_Export in ``source``.

        Args:
            source: Fitbit files, or one zip archive.
            tz_context: Display_Timezone and per-file-type overrides.
            selection: When given, the selected SleepSession: heart rate, steps,
                and HRV details files outside :func:`files_in_selection_window`
                (in ``tz_context.display_tz``) are not read, and one warning is
                added per metric missing from the session (Requirement 4.5, 4.6).

        Raises:
            User_Error: ``INVALID_TIMEZONE`` for an invalid Fitbit override
                (checked before anything is read); ``MULTIPLE_ARCHIVES``,
                ``ARCHIVE_UNREADABLE``, or ``NO_FITBIT_FILES`` from packaging.
        """
        zones = _resolve_zones(tz_context)
        key = _cache_key(source, tz_context)
        report = Import_Report()
        counter = DstAdjustmentCounter()
        with open_fitbit_export(source, report) as export:
            result = _load_export(export, zones, tz_context.display_tz, report, counter, selection)
        report.warnings.extend(counter.warnings())
        if selection is not None:
            add_missing_metric_warnings(
                report, selection, result.telemetry, result.session_hrv, tz_context.display_tz
            )
        self._remember(key, result.sessions)
        return result

    def candidate_sessions(self, source: Source, tz_context: TimezoneContext) -> list[SleepSession]:
        """The candidate SleepSessions of ``source``, equal to ``load(...).sessions``.

        Served from the most recent load of the same, unchanged source with the
        same timezone context; otherwise only the sleep files are read.

        Raises:
            User_Error: as for :meth:`load`.
        """
        zones = _resolve_zones(tz_context)
        key = _cache_key(source, tz_context)
        cached = self._cached(key)
        if cached is not None:
            return cached
        scratch = Import_Report()
        with open_fitbit_export(source, scratch) as export:
            result = _load_export(
                export, zones, tz_context.display_tz, scratch, DstAdjustmentCounter(), None, sleep_only=True
            )
        self._remember(key, result.sessions)
        return list(result.sessions)
