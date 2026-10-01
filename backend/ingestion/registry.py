"""Adapter registry, validation gate, and atomic import (Requirements 1.7, 7, 9.10, 16.5).

The registry maps a unique Source_Identifier to a
:class:`~backend.domain.adapter.Data_Source_Adapter`. Adapters are registered
at runtime (by ``backend.api.pipeline`` at startup); this module never imports
an Importer sub-package, so registering a new adapter is the only code a new
source needs (Requirement 7.4).

``run_import`` steps, in order
==============================

1. Validate every Source_Timezone override (and the Display_Timezone) before
   anything is read. An invalid IANA name raises ``INVALID_TIMEZONE`` and
   nothing is stored (Requirement 9.10).
2. Look up the adapter for the Source_Identifier
   (:class:`UnknownSourceError` if none is registered).
3. Call ``load`` and, when the adapter implements it, ``candidate_sessions``.
   An adapter without ``candidate_sessions`` contributes zero candidates
   (Requirement 7.2). A :class:`~backend.domain.errors.User_Error` raised by
   the adapter propagates unchanged; any other failure becomes a
   ``SOURCE_LOAD_FAILED`` User_Error naming the source files. Nothing is stored
   in either case (Requirement 7.6).
4. Validation gate (Requirement 7.7): drop every returned TelemetryPoint with a
   naive timestamp (or a UTC offset that is not a whole number of seconds,
   counted as ``naive_timestamp``), a non-MVP metric, a non-finite value, or an
   inconvertible unit (plus malformed objects and empty sources, which
   :func:`backend.domain.telemetry.is_valid` also rejects), keep the rest in
   order, and count each exclusion reason in
   ``Import_Report.excluded_point_counts``. Per-metric counts and coverage in
   the report are recomputed from the retained points.
5. Persist the retained points atomically with
   :class:`~backend.persistence.telemetry_store.TelemetryStore` (Requirement
   1.7, 1.9).
6. Record the import through the injected ``ImportStores.record_import``
   callable (wired to the Metadata_Store by the pipeline). If recording fails,
   the telemetry file written in step 5 is deleted, so nothing from the failed
   import stays in the Data_Directory (Requirement 16.5).

Logs carry metadata only (file names, counts, codes, ids, durations).

Dependency_Rules: imports the standard library, ``backend.domain`` and
``backend.persistence`` only.
"""

from __future__ import annotations

import logging
import math
import time
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any
from zoneinfo import ZoneInfo

from backend.domain.adapter import (
    SOURCE_SENSOR_SEPARATOR,
    Data_Source_Adapter,
    LoadResult,
    SessionHrvRow,
    Source,
    TimezoneContext,
    candidate_sessions_of,
    source_paths,
)
from backend.domain.errors import (
    SAVE_FAILED,
    SOURCE_LOAD_FAILED,
    Import_Report,
    User_Error,
    merge_import_reports,
)
from backend.domain.session import SleepSession
from backend.domain.telemetry import Metric, TelemetryPoint, is_valid
from backend.domain.timezones import validate_iana, validate_overrides
from backend.domain.units import is_convertible
from backend.persistence.safe_logging import get_logger
from backend.persistence.telemetry_store import TelemetryStore

__all__ = [
    "EXCLUDED_MALFORMED_POINT",
    "EXCLUDED_NAIVE_TIMESTAMP",
    "EXCLUDED_NON_MVP_METRIC",
    "EXCLUDED_NON_FINITE_VALUE",
    "EXCLUDED_INCONVERTIBLE_UNIT",
    "EXCLUDED_INVALID_SOURCE",
    "EXCLUSION_REASONS",
    "exclusion_reason",
    "GateResult",
    "apply_validation_gate",
    "UnknownSourceError",
    "ImportRecord",
    "RecordImport",
    "ImportStores",
    "ImportResult",
    "AdapterRegistry",
    "default_registry",
    "register_adapter",
    "get_adapter",
    "run_import",
]

_log = get_logger("ingestion")

# ---------------------------------------------------------------------------
# Validation gate (Requirement 7.7)
# ---------------------------------------------------------------------------

#: The returned object is not a TelemetryPoint at all.
EXCLUDED_MALFORMED_POINT = "malformed_point"
#: Timestamp missing, not a datetime, without timezone information, or with a
#: UTC offset that is not a whole number of seconds (e.g. ``+00:00:00.000001``).
#: A sub-second offset cannot be persisted exactly, so it counts as having no
#: usable timezone information (Requirement 1.2, 1.8).
EXCLUDED_NAIVE_TIMESTAMP = "naive_timestamp"
#: Metric is not one of the MVP :class:`Metric` values.
EXCLUDED_NON_MVP_METRIC = "non_mvp_metric"
#: Value is not a real number, is NaN or infinite, or is too large for a float.
EXCLUDED_NON_FINITE_VALUE = "non_finite_value"
#: Unit is not convertible to the metric's Canonical_Unit.
EXCLUDED_INCONVERTIBLE_UNIT = "inconvertible_unit"
#: ``source`` is not a non-empty string.
EXCLUDED_INVALID_SOURCE = "invalid_source"

#: Every exclusion reason, in the order the gate checks them.
EXCLUSION_REASONS: tuple[str, ...] = (
    EXCLUDED_MALFORMED_POINT,
    EXCLUDED_NAIVE_TIMESTAMP,
    EXCLUDED_NON_MVP_METRIC,
    EXCLUDED_NON_FINITE_VALUE,
    EXCLUDED_INCONVERTIBLE_UNIT,
    EXCLUDED_INVALID_SOURCE,
)


def _has_usable_offset(ts: Any) -> bool:
    """True iff ``ts`` is an aware datetime whose UTC offset is a whole number
    of seconds (the timestamp rule of :func:`~backend.domain.telemetry.is_valid`)."""
    if not isinstance(ts, datetime) or ts.tzinfo is None:
        return False
    try:
        offset = ts.utcoffset()
    except Exception:  # a broken tzinfo counts as no timezone information
        return False
    return offset is not None and offset.microseconds == 0


def _is_finite_number(value: Any) -> bool:
    # bool is an int subclass but not a measurement.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(float(value))
    except OverflowError:  # int too large to convert to the canonical float
        return False


def exclusion_reason(point: object) -> str | None:
    """The first reason ``point`` fails the validation gate, or ``None`` if it passes.

    Checks run in :data:`EXCLUSION_REASONS` order, so each excluded point has
    exactly one reason. A point passes iff it is a TelemetryPoint for which
    :func:`~backend.domain.telemetry.is_valid` holds. Never raises.
    """
    if not isinstance(point, TelemetryPoint):
        return EXCLUDED_MALFORMED_POINT
    if not _has_usable_offset(point.timestamp):
        return EXCLUDED_NAIVE_TIMESTAMP
    if not isinstance(point.metric, Metric):
        return EXCLUDED_NON_MVP_METRIC
    if not _is_finite_number(point.value):
        return EXCLUDED_NON_FINITE_VALUE
    if not is_convertible(point.metric, point.unit):
        return EXCLUDED_INCONVERTIBLE_UNIT
    if not isinstance(point.source, str) or point.source == "":
        return EXCLUDED_INVALID_SOURCE
    try:
        valid = is_valid(point)
    except Exception:
        valid = False
    return None if valid else EXCLUDED_MALFORMED_POINT


@dataclass(frozen=True)
class GateResult:
    """Output of :func:`apply_validation_gate`.

    Attributes:
        retained: The valid points, in their original order.
        excluded: Exclusion reason -> number of points excluded for it
            (only reasons with a non-zero count appear).
    """

    retained: tuple[TelemetryPoint, ...]
    excluded: Mapping[str, int]

    @property
    def excluded_count(self) -> int:
        return sum(self.excluded.values())


def apply_validation_gate(points: Iterable[object]) -> GateResult:
    """Split ``points`` into retained valid points and per-reason exclusion counts."""
    retained: list[TelemetryPoint] = []
    excluded: dict[str, int] = {}
    for point in points:
        reason = exclusion_reason(point)
        if reason is None:
            retained.append(point)  # type: ignore[arg-type]
        else:
            excluded[reason] = excluded.get(reason, 0) + 1
    return GateResult(tuple(retained), MappingProxyType(excluded))


def _utc(ts: datetime) -> datetime:
    return ts.astimezone(timezone.utc)


def _gated_report(adapter_report: Import_Report, gate: GateResult) -> Import_Report:
    """Copy of the adapter's report with gate counts added and per-metric counts
    and coverage recomputed from the retained points."""
    report = merge_import_reports([adapter_report])  # deep enough copy; input untouched
    for reason, count in gate.excluded.items():
        report.add_excluded(reason, count)

    counts: dict[str, int] = {}
    coverage: dict[str, tuple[datetime, datetime]] = {}
    for p in gate.retained:
        m = p.metric.value
        counts[m] = counts.get(m, 0) + 1
        first, last = coverage.get(m, (p.timestamp, p.timestamp))
        # Compare UTC instants: same-ZoneInfo comparison ignores fold across DST fall-back.
        coverage[m] = (min(first, p.timestamp, key=_utc), max(last, p.timestamp, key=_utc))

    # Keep metrics the adapter reported (as zero if nothing survived), then add new ones.
    metric_keys = [str(getattr(k, "value", k)) for k in adapter_report.per_metric_counts]
    metric_keys += [str(getattr(k, "value", k)) for k in adapter_report.per_metric_coverage]
    metric_keys += [m.value for m in Metric if m.value in counts]
    report.per_metric_counts = {}
    report.per_metric_coverage = {}
    for m in dict.fromkeys(metric_keys):
        report.per_metric_counts[m] = counts.get(m, 0)
        report.per_metric_coverage[m] = coverage.get(m)
    return report


# ---------------------------------------------------------------------------
# Stores and records
# ---------------------------------------------------------------------------


class UnknownSourceError(LookupError):
    """No adapter is registered for the requested Source_Identifier."""

    def __init__(self, source_identifier: str, known: Sequence[str] = ()) -> None:
        self.source_identifier = source_identifier
        self.known = tuple(known)
        super().__init__(f"no adapter registered for source {source_identifier!r}; known: {list(self.known)}")


@dataclass(frozen=True)
class ImportRecord:
    """Everything the Metadata_Store needs to record one import.

    Holds no telemetry values: those live in the telemetry file named by
    ``telemetry_ref``.

    Attributes:
        import_id: Unique id; also the telemetry file stem.
        source_identifier: The adapter's Source_Identifier.
        source_files: File names (final path components) given as the source.
        telemetry_ref: Data_Directory-relative path of the telemetry file.
        point_count: Number of TelemetryPoints persisted.
        report: The final Import_Report (after the validation gate).
        candidate_sessions: Candidate SleepSessions from the adapter.
        session_hrv: Daily HRV summary rows from the adapter.
        display_timezone: IANA name of the Display_Timezone at import start.
        source_timezone_overrides: File type -> IANA override used.
    """

    import_id: str
    source_identifier: str
    source_files: tuple[str, ...]
    telemetry_ref: str
    point_count: int
    report: Import_Report
    candidate_sessions: tuple[SleepSession, ...] = ()
    session_hrv: tuple[SessionHrvRow, ...] = ()
    display_timezone: str = "UTC"
    source_timezone_overrides: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "source_timezone_overrides", MappingProxyType(dict(self.source_timezone_overrides))
        )


#: Persists an :class:`ImportRecord` transactionally (e.g. ``MetadataStore.record_import``).
#: It must either store the whole record or raise, leaving earlier rows unchanged.
RecordImport = Callable[[ImportRecord], object]


@dataclass(frozen=True)
class ImportStores:
    """Where ``run_import`` writes.

    Attributes:
        telemetry: Per-import telemetry files in the Data_Directory.
        record_import: Records the import in the Metadata_Store (see :data:`RecordImport`).
    """

    telemetry: TelemetryStore
    record_import: RecordImport


@dataclass(frozen=True)
class ImportResult:
    """Outcome of a successful :func:`run_import`.

    Attributes:
        record: The stored :class:`ImportRecord` (includes the Import_Report).
        telemetry: The persisted (validated) TelemetryPoints, in source units.
    """

    record: ImportRecord
    telemetry: tuple[TelemetryPoint, ...]

    @property
    def import_id(self) -> str:
        return self.record.import_id

    @property
    def report(self) -> Import_Report:
        return self.record.report

    @property
    def candidate_sessions(self) -> tuple[SleepSession, ...]:
        return self.record.candidate_sessions

    @property
    def session_hrv(self) -> tuple[SessionHrvRow, ...]:
        return self.record.session_hrv


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


def _source_file_names(source: Source) -> tuple[str, ...]:
    try:
        return tuple(p.name or str(p) for p in source_paths(source))
    except TypeError:
        return ()


def _source_load_failed(source_identifier: str, file_names: Sequence[str]) -> User_Error:
    names = ", ".join(file_names)
    subject = f" from {names}" if names else ""
    return User_Error(
        SOURCE_LOAD_FAILED,
        description=f"The {source_identifier} data{subject} could not be loaded. Nothing from this import was saved.",
        action=(
            "Check that the files are complete, unmodified exports of a supported type, "
            "then import them again."
        ),
        file_name=names or None,
        details={"source": source_identifier, **({"source_files": names} if names else {})},
    )


def _record_failed(import_id: str, file_names: Sequence[str]) -> User_Error:
    names = ", ".join(file_names)
    return User_Error(
        SAVE_FAILED,
        description="The import could not be recorded in the Data_Directory. Nothing from this import was saved.",
        action="Make sure the Data_Directory is writable and has free disk space, then import the files again.",
        file_name=names or None,
        details={"import_id": import_id, **({"source_files": names} if names else {})},
    )


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class AdapterRegistry:
    """Source_Identifier -> Data_Source_Adapter, plus the import orchestration."""

    def __init__(self) -> None:
        self._adapters: dict[str, Data_Source_Adapter] = {}

    # -- registration ---------------------------------------------------------

    def register(self, adapter: Data_Source_Adapter, *, replace: bool = False) -> Data_Source_Adapter:
        """Register ``adapter`` under its ``source_identifier``.

        Raises:
            TypeError: ``adapter`` lacks ``source_identifier`` or a callable ``load``.
            ValueError: the identifier is empty, contains
                :data:`~backend.domain.adapter.SOURCE_SENSOR_SEPARATOR`, or is
                already registered (unless ``replace=True``).
        """
        if not isinstance(adapter, Data_Source_Adapter) or not callable(getattr(adapter, "load", None)):
            raise TypeError("adapter must have a source_identifier and a load(source, tz_context) method")
        sid = adapter.source_identifier
        if not isinstance(sid, str) or not sid.strip() or sid != sid.strip() or SOURCE_SENSOR_SEPARATOR in sid:
            raise ValueError(f"invalid Source_Identifier: {sid!r}")
        if sid in self._adapters and not replace:
            raise ValueError(f"an adapter is already registered for Source_Identifier {sid!r}")
        self._adapters[sid] = adapter
        return adapter

    def unregister(self, source_identifier: str) -> bool:
        """Remove the adapter for ``source_identifier``. Returns False if none was registered."""
        return self._adapters.pop(source_identifier, None) is not None

    def get(self, source_identifier: str) -> Data_Source_Adapter:
        """The adapter for ``source_identifier``; raises :class:`UnknownSourceError`."""
        try:
            return self._adapters[source_identifier]
        except (KeyError, TypeError):
            raise UnknownSourceError(str(source_identifier), self.source_identifiers()) from None

    def __contains__(self, source_identifier: object) -> bool:
        return source_identifier in self._adapters

    def __len__(self) -> int:
        return len(self._adapters)

    def source_identifiers(self) -> tuple[str, ...]:
        """Registered Source_Identifiers, sorted."""
        return tuple(sorted(self._adapters))

    # -- import -----------------------------------------------------------------

    def run_import(
        self,
        source_id: str,
        source: Source,
        tz_overrides: Mapping[str, str] | None,
        display_tz: ZoneInfo | str,
        stores: ImportStores,
        *,
        import_id: str | None = None,
    ) -> ImportResult:
        """Import ``source`` with the adapter for ``source_id`` and persist it atomically.

        See the module docstring for the step order.

        Args:
            source_id: Source_Identifier of a registered adapter.
            source: What the adapter reads (file paths or one zip).
            tz_overrides: File type -> IANA Source_Timezone override, or None.
            display_tz: The Display_Timezone in effect at import start.
            stores: Telemetry store and import-record callable.
            import_id: Optional explicit id (must be a safe, unused id);
                a random one is generated by default.

        Returns:
            The :class:`ImportResult`, after telemetry and record are stored.

        Raises:
            User_Error: ``INVALID_TIMEZONE`` (bad override or Display_Timezone),
                the adapter's own code or ``SOURCE_LOAD_FAILED`` (whole-source
                failure), or ``SAVE_FAILED`` (telemetry or record could not be
                stored). Nothing from the import is left in the Data_Directory.
            ValueError: an override key is not a known file type, or
                ``import_id`` is unsafe or already used.
            UnknownSourceError: no adapter is registered for ``source_id``.
        """
        started = time.monotonic()
        file_names = _source_file_names(source)

        # 1. Timezones first: nothing is read before every override is valid.
        validated = validate_overrides(tz_overrides)
        display_zone = display_tz if isinstance(display_tz, ZoneInfo) else validate_iana(display_tz)
        tz_context = TimezoneContext(display_zone, {ft: zone.key for ft, zone in validated.items()})

        # 2. Adapter and id (both checked before reading the source).
        adapter = self.get(source_id)
        if import_id is None:
            import_id = uuid.uuid4().hex
        TelemetryStore.validate_import_id(import_id)
        if stores.telemetry.exists(import_id):
            raise ValueError("import_id is already in use")

        _log.info("import.started", import_id=import_id, source_files=list(file_names))

        # 3. Load (whole-source failures store nothing).
        try:
            loaded = adapter.load(source, tz_context)
            if not isinstance(loaded, LoadResult):
                raise TypeError("load() must return a LoadResult")
            candidates = tuple(candidate_sessions_of(adapter, source, tz_context))
        except User_Error as exc:
            _log.warning("import.failed", import_id=import_id, error_code=exc.code, source_files=list(file_names))
            raise
        except Exception as exc:
            error = _source_load_failed(source_id, file_names)
            _log.exception("import.failed", exc, level=logging.WARNING, import_id=import_id,
                           error_code=error.code, source_files=list(file_names))
            raise error from None

        # 4. Validation gate.
        gate = apply_validation_gate(loaded.telemetry)
        report = _gated_report(loaded.report, gate)

        # 5. Telemetry file (atomic; SAVE_FAILED leaves no file behind).
        try:
            telemetry_ref = stores.telemetry.save(import_id, gate.retained, file_names)
        except User_Error as exc:
            _log.error("import.failed", import_id=import_id, error_code=exc.code, source_files=list(file_names))
            raise

        record = ImportRecord(
            import_id=import_id,
            source_identifier=source_id,
            source_files=file_names,
            telemetry_ref=telemetry_ref,
            point_count=len(gate.retained),
            report=report,
            candidate_sessions=candidates,
            session_hrv=tuple(loaded.session_hrv),
            display_timezone=display_zone.key,
            source_timezone_overrides=tz_context.overrides,
        )

        # 6. Metadata record; on failure remove the telemetry file (atomic import).
        try:
            stores.record_import(record)
        except Exception as exc:
            try:
                stores.telemetry.delete(import_id)
            except Exception as cleanup_exc:  # pragma: no cover - best effort, logged
                _log.exception("import.cleanup_failed", cleanup_exc, import_id=import_id)
            error = exc if isinstance(exc, User_Error) else _record_failed(import_id, file_names)
            _log.exception("import.failed", exc, import_id=import_id, error_code=error.code,
                           source_files=list(file_names))
            if error is exc:
                raise
            raise error from None

        _log.info(
            "import.completed",
            import_id=import_id,
            source_files=list(file_names),
            point_count=len(gate.retained),
            excluded_count=gate.excluded_count,
            candidate_count=len(candidates),
            duration_ms=(time.monotonic() - started) * 1000.0,
        )
        return ImportResult(record=record, telemetry=gate.retained)


# ---------------------------------------------------------------------------
# Process-wide default registry
# ---------------------------------------------------------------------------

#: The registry used by the Backend (adapters are registered by ``backend.api.pipeline``).
default_registry = AdapterRegistry()


def register_adapter(adapter: Data_Source_Adapter, *, replace: bool = False) -> Data_Source_Adapter:
    """Register ``adapter`` with :data:`default_registry`."""
    return default_registry.register(adapter, replace=replace)


def get_adapter(source_identifier: str) -> Data_Source_Adapter:
    """Look up an adapter in :data:`default_registry`."""
    return default_registry.get(source_identifier)


def run_import(
    source_id: str,
    source: Source,
    tz_overrides: Mapping[str, str] | None,
    display_tz: ZoneInfo | str,
    stores: ImportStores,
    *,
    import_id: str | None = None,
) -> ImportResult:
    """:meth:`AdapterRegistry.run_import` on :data:`default_registry`."""
    return default_registry.run_import(source_id, source, tz_overrides, display_tz, stores, import_id=import_id)
