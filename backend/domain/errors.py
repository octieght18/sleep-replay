"""Structured errors, warnings, and reports (Requirement 16).

Every failure the user can act on is a :class:`User_Error` carrying a stable
error code from :data:`ERROR_CODE_TABLE`, a plain-language description, the
affected file or archive member name when applicable, and the action the user
must take (Requirement 16.1). Conditions that still let an operation complete
become :class:`Warning_Item` entries in an :class:`Import_Report` (import and
session-discovery conditions) or a :class:`Processing_Report` (alignment
conditions) (Requirement 16.4).

This module uses the standard library only, so it can be imported from any
Backend package without breaking the Dependency_Rules. Metric keys are typed as
``str``; the domain ``Metric`` enum is a ``str`` subclass, so its members work
as keys interchangeably with their string values.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

__all__ = [
    "ErrorCode",
    "ERROR_CODE_TABLE",
    "ERROR_CODES",
    "NO_FITBIT_FILES",
    "MULTIPLE_ARCHIVES",
    "ARCHIVE_UNREADABLE",
    "NO_TIMESTAMP_COLUMN",
    "NO_READABLE_ROWS",
    "INVALID_TIMEZONE",
    "SESSION_TOO_SHORT",
    "INVALID_TARGET_DURATION",
    "NO_SLEEP_SESSION",
    "INVALID_MANUAL_RANGE",
    "SAVE_FAILED",
    "PERSISTED_DATA_UNREADABLE",
    "DATA_DIR_UNWRITABLE",
    "INSUFFICIENT_DATA",
    "SOURCE_LOAD_FAILED",
    "INVALID_MAPPING_CONFIG", "INVALID_RANDOM_SEED", "NO_USABLE_DATA",
    "RENDERING_FAILED", "REPLAY_WRITE_FAILED", "GENERATION_IN_PROGRESS",
    "NO_ACTION_REQUIRED",
    "User_Error",
    "Warning_Item",
    "Import_Report",
    "Processing_Report",
    "merge_import_reports",
    "merge_processing_reports",
]


# ---------------------------------------------------------------------------
# Error-code table
# ---------------------------------------------------------------------------


class ErrorCode(StrEnum):
    """Stable error codes. Each value is identical for every occurrence of the
    same condition and is listed in the Documentation troubleshooting section."""

    INVALID_MAPPING_CONFIG = "INVALID_MAPPING_CONFIG"
    INVALID_RANDOM_SEED = "INVALID_RANDOM_SEED"
    NO_USABLE_DATA = "NO_USABLE_DATA"
    RENDERING_FAILED = "RENDERING_FAILED"
    REPLAY_WRITE_FAILED = "REPLAY_WRITE_FAILED"
    GENERATION_IN_PROGRESS = "GENERATION_IN_PROGRESS"
    NO_FITBIT_FILES = "NO_FITBIT_FILES"
    MULTIPLE_ARCHIVES = "MULTIPLE_ARCHIVES"
    ARCHIVE_UNREADABLE = "ARCHIVE_UNREADABLE"
    NO_TIMESTAMP_COLUMN = "NO_TIMESTAMP_COLUMN"
    NO_READABLE_ROWS = "NO_READABLE_ROWS"
    INVALID_TIMEZONE = "INVALID_TIMEZONE"
    SESSION_TOO_SHORT = "SESSION_TOO_SHORT"
    INVALID_TARGET_DURATION = "INVALID_TARGET_DURATION"
    NO_SLEEP_SESSION = "NO_SLEEP_SESSION"
    INVALID_MANUAL_RANGE = "INVALID_MANUAL_RANGE"
    SAVE_FAILED = "SAVE_FAILED"
    PERSISTED_DATA_UNREADABLE = "PERSISTED_DATA_UNREADABLE"
    DATA_DIR_UNWRITABLE = "DATA_DIR_UNWRITABLE"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    SOURCE_LOAD_FAILED = "SOURCE_LOAD_FAILED"


# The fixed table: code -> condition summary (for the troubleshooting docs).
ERROR_CODE_TABLE: Mapping[ErrorCode, str] = {
    ErrorCode.INVALID_MAPPING_CONFIG: "The mapping configuration contains invalid keys or values.",
    ErrorCode.INVALID_RANDOM_SEED: "The random seed is not an integer from 0 to 4294967295.",
    ErrorCode.NO_USABLE_DATA: "The selected session has no sleep stages, heart rate, or environmental data.",
    ErrorCode.RENDERING_FAILED: "Audio could not be rendered within the output limits.",
    ErrorCode.REPLAY_WRITE_FAILED: "The replay artifacts or metadata could not be stored.",
    ErrorCode.GENERATION_IN_PROGRESS: "A replay generation is already in progress.",
    ErrorCode.NO_FITBIT_FILES: "The import contains no file matching a supported Fitbit file name pattern.",
    ErrorCode.MULTIPLE_ARCHIVES: "More than one archive, or an archive together with individual files, was provided.",
    ErrorCode.ARCHIVE_UNREADABLE: "The archive could not be opened or read.",
    ErrorCode.NO_TIMESTAMP_COLUMN: "The SensorPush file has no timestamp column or no temperature, humidity, or pressure column.",
    ErrorCode.NO_READABLE_ROWS: "The SensorPush file has no data rows, or no row produced a measurement.",
    ErrorCode.INVALID_TIMEZONE: "A timezone override is not a valid IANA timezone name.",
    ErrorCode.SESSION_TOO_SHORT: "The sleep session is not longer than the requested Target_Duration.",
    ErrorCode.INVALID_TARGET_DURATION: "The requested Target_Duration is not one of the allowed values.",
    ErrorCode.NO_SLEEP_SESSION: "No candidate sleep session was found in any import.",
    ErrorCode.INVALID_MANUAL_RANGE: "The manually entered sleep session start and end times are missing or invalid.",
    ErrorCode.SAVE_FAILED: "Imported telemetry could not be saved to the Data_Directory.",
    ErrorCode.PERSISTED_DATA_UNREADABLE: "Previously saved telemetry could not be read.",
    ErrorCode.DATA_DIR_UNWRITABLE: "The Data_Directory cannot be created or is not writable.",
    ErrorCode.INSUFFICIENT_DATA: "The selected sleep session has no stage, heart rate, or environmental data.",
    ErrorCode.SOURCE_LOAD_FAILED: "The data source could not be loaded.",
}

ERROR_CODES: frozenset[str] = frozenset(code.value for code in ErrorCode)

# Plain string constants, for callers that prefer them to the enum.
NO_FITBIT_FILES: str = ErrorCode.NO_FITBIT_FILES.value
MULTIPLE_ARCHIVES: str = ErrorCode.MULTIPLE_ARCHIVES.value
ARCHIVE_UNREADABLE: str = ErrorCode.ARCHIVE_UNREADABLE.value
NO_TIMESTAMP_COLUMN: str = ErrorCode.NO_TIMESTAMP_COLUMN.value
NO_READABLE_ROWS: str = ErrorCode.NO_READABLE_ROWS.value
INVALID_TIMEZONE: str = ErrorCode.INVALID_TIMEZONE.value
SESSION_TOO_SHORT: str = ErrorCode.SESSION_TOO_SHORT.value
INVALID_TARGET_DURATION: str = ErrorCode.INVALID_TARGET_DURATION.value
NO_SLEEP_SESSION: str = ErrorCode.NO_SLEEP_SESSION.value
INVALID_MANUAL_RANGE: str = ErrorCode.INVALID_MANUAL_RANGE.value
SAVE_FAILED: str = ErrorCode.SAVE_FAILED.value
PERSISTED_DATA_UNREADABLE: str = ErrorCode.PERSISTED_DATA_UNREADABLE.value
DATA_DIR_UNWRITABLE: str = ErrorCode.DATA_DIR_UNWRITABLE.value
INSUFFICIENT_DATA: str = ErrorCode.INSUFFICIENT_DATA.value
SOURCE_LOAD_FAILED: str = ErrorCode.SOURCE_LOAD_FAILED.value
INVALID_MAPPING_CONFIG: str = ErrorCode.INVALID_MAPPING_CONFIG.value
INVALID_RANDOM_SEED: str = ErrorCode.INVALID_RANDOM_SEED.value
NO_USABLE_DATA: str = ErrorCode.NO_USABLE_DATA.value
RENDERING_FAILED: str = ErrorCode.RENDERING_FAILED.value
REPLAY_WRITE_FAILED: str = ErrorCode.REPLAY_WRITE_FAILED.value
GENERATION_IN_PROGRESS: str = ErrorCode.GENERATION_IN_PROGRESS.value

# Recommended action text for warnings that need no user action (Req 16.4).
NO_ACTION_REQUIRED = "No action is required."


# ---------------------------------------------------------------------------
# User_Error
# ---------------------------------------------------------------------------

_USER_ERROR_FIELDS = frozenset({"code", "description", "action", "file_name", "details"})


class User_Error(Exception):  # noqa: N801 - name follows the spec glossary
    """A structured, user-actionable error (Requirement 16.1).

    Raisable like any exception and serializable with :meth:`to_dict`.
    Fields are read-only after construction.

    Args:
        code: A code from :class:`ErrorCode` (enum member or its string value).
        description: Plain-language problem description. Must not contain a
            stack trace, exception type name, or source code reference.
        action: What the user needs to do.
        file_name: Affected file or archive member name, when applicable.
        details: Optional extra context as string pairs (for example a
            rejected timezone value or detected column headers).

    Raises:
        ValueError: If ``code`` is not in the error-code table, or
            ``description`` / ``action`` is empty.
    """

    code: str
    description: str
    action: str
    file_name: str | None
    details: Mapping[str, str]

    def __init__(
        self,
        code: ErrorCode | str,
        description: str,
        action: str,
        file_name: str | None = None,
        details: Mapping[str, str] | None = None,
    ) -> None:
        code_str = str(code.value if isinstance(code, ErrorCode) else code)
        if code_str not in ERROR_CODES:
            raise ValueError(f"unknown error code: {code_str!r}")
        if not description or not description.strip():
            raise ValueError("description must be non-empty")
        if not action or not action.strip():
            raise ValueError("action must be non-empty")
        super().__init__(code_str, description, action, file_name)
        object.__setattr__(self, "code", code_str)
        object.__setattr__(self, "description", description)
        object.__setattr__(self, "action", action)
        object.__setattr__(self, "file_name", file_name)
        object.__setattr__(self, "details", dict(details or {}))

    def __setattr__(self, name: str, value: Any) -> None:
        # Fields are immutable; exception machinery attributes stay writable.
        if name in _USER_ERROR_FIELDS:
            raise AttributeError(f"User_Error.{name} is read-only")
        super().__setattr__(name, value)

    @property
    def error_code(self) -> ErrorCode:
        """The code as an :class:`ErrorCode` member."""
        return ErrorCode(self.code)

    def __str__(self) -> str:
        subject = f" ({self.file_name})" if self.file_name else ""
        return f"[{self.code}] {self.description}{subject} {self.action}"

    def __repr__(self) -> str:
        return (
            f"User_Error(code={self.code!r}, description={self.description!r}, "
            f"action={self.action!r}, file_name={self.file_name!r}, details={dict(self.details)!r})"
        )

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, User_Error):
            return NotImplemented
        return self.to_dict() == other.to_dict()

    def __hash__(self) -> int:
        return hash((self.code, self.description, self.action, self.file_name, tuple(sorted(self.details.items()))))

    def __reduce__(self):  # keep pickling working with the keyword-free constructor
        return (type(self), (self.code, self.description, self.action, self.file_name, dict(self.details)))

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable representation."""
        return {
            "code": self.code,
            "description": self.description,
            "action": self.action,
            "file_name": self.file_name,
            "details": dict(self.details),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> User_Error:
        return cls(
            data["code"],
            data["description"],
            data["action"],
            data.get("file_name"),
            data.get("details") or None,
        )


# ---------------------------------------------------------------------------
# Warnings
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Warning_Item:  # noqa: N801
    """A non-fatal condition (Requirement 16.4).

    Attributes:
        description: Plain-language description of the condition.
        subject: Affected file name or metric, if any.
        recommended_action: What the user should do, or :data:`NO_ACTION_REQUIRED`.
    """

    description: str
    subject: str | None = None
    recommended_action: str = NO_ACTION_REQUIRED

    def to_dict(self) -> dict[str, Any]:
        return {
            "description": self.description,
            "subject": None if self.subject is None else str(self.subject),
            "recommended_action": self.recommended_action,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Warning_Item:
        return cls(
            description=data["description"],
            subject=data.get("subject"),
            recommended_action=data.get("recommended_action", NO_ACTION_REQUIRED),
        )


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------

Coverage = tuple[datetime, datetime] | None


def _json_value(value: Any) -> Any:
    """Convert report values (datetimes, timedeltas, enums, tuples) to JSON-safe forms."""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, timedelta):
        return value.total_seconds()
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, Mapping):
        return {str(_json_value(k)): _json_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(v) for v in value]
    return value


def _union_coverage(a: Coverage, b: Coverage) -> Coverage:
    if a is None:
        return b
    if b is None:
        return a
    return (min(a[0], b[0]), max(a[1], b[1]))


def _sum_counts(target: dict[Any, int], source: Mapping[Any, int]) -> None:
    for key, count in source.items():
        target[key] = target.get(key, 0) + count


# ---------------------------------------------------------------------------
# Import_Report
# ---------------------------------------------------------------------------


@dataclass
class Import_Report:  # noqa: N801
    """Summary of one import. Holds counts, names, coverage bounds, and
    warnings only; it never holds telemetry values.

    Attributes:
        accepted_files: Names of files (or archive members) that were imported.
        skipped_files: ``(name, reason)`` for each invalid file (Req 16.2).
        unsupported_files: Names that matched no supported file pattern.
        unsupported_count: Number of unsupported files.
        skipped_value_count: Values skipped as unparseable/empty/out of range.
        skipped_row_count: Rows skipped (for example unparseable timestamps).
        per_metric_counts: Metric -> number of TelemetryPoints imported.
        per_metric_coverage: Metric -> ``(first, last)`` instant, or ``None``.
        applied_source_timezones: File type -> IANA timezone applied (Req 9.1).
        discarded_duplicate_sessions: Duplicate candidate sessions dropped.
        excluded_point_counts: Validation-gate exclusion reason -> count (Req 7.7).
        warnings: Non-fatal conditions (Req 16.4).
    """

    accepted_files: list[str] = field(default_factory=list)
    skipped_files: list[tuple[str, str]] = field(default_factory=list)
    unsupported_files: list[str] = field(default_factory=list)
    unsupported_count: int = 0
    skipped_value_count: int = 0
    skipped_row_count: int = 0
    per_metric_counts: dict[str, int] = field(default_factory=dict)
    per_metric_coverage: dict[str, Coverage] = field(default_factory=dict)
    applied_source_timezones: dict[str, str] = field(default_factory=dict)
    discarded_duplicate_sessions: int = 0
    excluded_point_counts: dict[str, int] = field(default_factory=dict)
    warnings: list[Warning_Item] = field(default_factory=list)

    # -- mutation helpers used by importers ---------------------------------

    def add_warning(
        self,
        description: str,
        subject: str | None = None,
        recommended_action: str = NO_ACTION_REQUIRED,
    ) -> Warning_Item:
        item = Warning_Item(description, subject, recommended_action)
        self.warnings.append(item)
        return item

    def skip_file(self, name: str, reason: str) -> None:
        self.skipped_files.append((name, reason))

    def add_unsupported_file(self, name: str) -> None:
        self.unsupported_files.append(name)
        self.unsupported_count += 1

    def record_metric(self, metric: str, count: int, coverage: Coverage) -> None:
        """Add ``count`` points for ``metric`` and widen its coverage."""
        self.per_metric_counts[metric] = self.per_metric_counts.get(metric, 0) + count
        self.per_metric_coverage[metric] = _union_coverage(self.per_metric_coverage.get(metric), coverage)

    def add_excluded(self, reason: str, count: int = 1) -> None:
        self.excluded_point_counts[reason] = self.excluded_point_counts.get(reason, 0) + count

    # -- combination --------------------------------------------------------

    def merge(self, other: Import_Report) -> Import_Report:
        """Return a new report combining ``self`` and ``other`` (``other``'s
        timezone entries win on conflicts). Neither input is modified."""
        return merge_import_reports([self, other])

    # -- serialization ------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable representation (datetimes as ISO 8601 strings)."""
        return {
            "accepted_files": list(self.accepted_files),
            "skipped_files": [{"name": n, "reason": r} for n, r in self.skipped_files],
            "unsupported_files": list(self.unsupported_files),
            "unsupported_count": self.unsupported_count,
            "skipped_value_count": self.skipped_value_count,
            "skipped_row_count": self.skipped_row_count,
            "per_metric_counts": _json_value(self.per_metric_counts),
            "per_metric_coverage": {
                str(_json_value(m)): None if cov is None else {"start": cov[0].isoformat(), "end": cov[1].isoformat()}
                for m, cov in self.per_metric_coverage.items()
            },
            "applied_source_timezones": dict(self.applied_source_timezones),
            "discarded_duplicate_sessions": self.discarded_duplicate_sessions,
            "excluded_point_counts": _json_value(self.excluded_point_counts),
            "warnings": [w.to_dict() for w in self.warnings],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Import_Report:
        """Inverse of :meth:`to_dict`. Metric keys come back as plain strings."""
        coverage: dict[str, Coverage] = {}
        for metric, cov in (data.get("per_metric_coverage") or {}).items():
            coverage[metric] = (
                None if cov is None else (datetime.fromisoformat(cov["start"]), datetime.fromisoformat(cov["end"]))
            )
        return cls(
            accepted_files=list(data.get("accepted_files", [])),
            skipped_files=[(s["name"], s["reason"]) for s in data.get("skipped_files", [])],
            unsupported_files=list(data.get("unsupported_files", [])),
            unsupported_count=int(data.get("unsupported_count", 0)),
            skipped_value_count=int(data.get("skipped_value_count", 0)),
            skipped_row_count=int(data.get("skipped_row_count", 0)),
            per_metric_counts={k: int(v) for k, v in (data.get("per_metric_counts") or {}).items()},
            per_metric_coverage=coverage,
            applied_source_timezones=dict(data.get("applied_source_timezones") or {}),
            discarded_duplicate_sessions=int(data.get("discarded_duplicate_sessions", 0)),
            excluded_point_counts={k: int(v) for k, v in (data.get("excluded_point_counts") or {}).items()},
            warnings=[Warning_Item.from_dict(w) for w in data.get("warnings", [])],
        )


def merge_import_reports(reports: Iterable[Import_Report]) -> Import_Report:
    """Combine reports in order: lists are concatenated, counts summed,
    coverage unioned, and later timezone entries override earlier ones.
    Inputs are not modified; an empty iterable gives an empty report."""
    merged = Import_Report()
    for r in reports:
        merged.accepted_files.extend(r.accepted_files)
        merged.skipped_files.extend(r.skipped_files)
        merged.unsupported_files.extend(r.unsupported_files)
        merged.unsupported_count += r.unsupported_count
        merged.skipped_value_count += r.skipped_value_count
        merged.skipped_row_count += r.skipped_row_count
        _sum_counts(merged.per_metric_counts, r.per_metric_counts)
        for metric, cov in r.per_metric_coverage.items():
            merged.per_metric_coverage[metric] = _union_coverage(merged.per_metric_coverage.get(metric), cov)
        merged.applied_source_timezones.update(r.applied_source_timezones)
        merged.discarded_duplicate_sessions += r.discarded_duplicate_sessions
        _sum_counts(merged.excluded_point_counts, r.excluded_point_counts)
        merged.warnings.extend(r.warnings)
    return merged


# ---------------------------------------------------------------------------
# Processing_Report
# ---------------------------------------------------------------------------


@dataclass
class Processing_Report:  # noqa: N801
    """Summary of alignment and processing.

    Attributes:
        removed_duplicates: ``(source, metric)`` -> removed duplicate count (Req 10.2).
        gaps: One dict per gap: metric, last-value timestamp, elapsed (Req 10.9).
        unavailable_metrics: Metrics with no data for the session (Req 10.10).
        sensor_selection: One dict per metric: metric, selected, ignored (Req 10.13).
        warnings: Non-fatal alignment conditions (Req 16.4).
    """

    removed_duplicates: dict[tuple[str, str], int] = field(default_factory=dict)
    gaps: list[dict[str, Any]] = field(default_factory=list)
    unavailable_metrics: list[str] = field(default_factory=list)
    sensor_selection: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[Warning_Item] = field(default_factory=list)

    def add_warning(
        self,
        description: str,
        subject: str | None = None,
        recommended_action: str = NO_ACTION_REQUIRED,
    ) -> Warning_Item:
        item = Warning_Item(description, subject, recommended_action)
        self.warnings.append(item)
        return item

    def merge(self, other: Processing_Report) -> Processing_Report:
        """Return a new report combining ``self`` and ``other``."""
        return merge_processing_reports([self, other])

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable representation (datetimes as ISO 8601, timedeltas as seconds)."""
        return {
            "removed_duplicates": [
                {"source": str(_json_value(src)), "metric": str(_json_value(metric)), "count": count}
                for (src, metric), count in self.removed_duplicates.items()
            ],
            "gaps": [_json_value(g) for g in self.gaps],
            "unavailable_metrics": [str(_json_value(m)) for m in self.unavailable_metrics],
            "sensor_selection": [_json_value(s) for s in self.sensor_selection],
            "warnings": [w.to_dict() for w in self.warnings],
        }


def merge_processing_reports(reports: Iterable[Processing_Report]) -> Processing_Report:
    """Combine reports in order: duplicate counts summed, lists concatenated,
    unavailable metrics unioned (first-seen order). Inputs are not modified."""
    merged = Processing_Report()
    for r in reports:
        _sum_counts(merged.removed_duplicates, r.removed_duplicates)
        merged.gaps.extend(dict(g) for g in r.gaps)
        for metric in r.unavailable_metrics:
            if metric not in merged.unavailable_metrics:
                merged.unavailable_metrics.append(metric)
        merged.sensor_selection.extend(dict(s) for s in r.sensor_selection)
        merged.warnings.extend(r.warnings)
    return merged
