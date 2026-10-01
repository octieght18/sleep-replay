"""Metadata-only logging (Requirement 15.1, 15.3).

Log output, at every level and including exception traces and request logs,
is restricted to operational metadata: operation names, file names, record
counts, durations, error codes, reference identifiers, and warning / error
descriptions. Telemetry values, Stage_Segments, per-sample timestamps, and
imported file contents are never written.

The helper enforces this structurally rather than by convention:

* :class:`SafeLogger` accepts only an operation name plus keyword fields from
  :data:`ALLOWED_FIELDS` (or any ``*_count`` name). Each field has a kind with
  a type check: counts are non-negative ints, durations are finite
  non-negative numbers, error codes must be in the error-code table, reference
  ids must look like identifiers, and names / descriptions are bounded strings.
  There is no free-form message argument. Unknown fields and values of the
  wrong type are dropped (only their number is logged, never their names or
  values); with ``strict=True`` they raise :class:`LogFieldError` instead.
* Every string that reaches the log has date-time and epoch-timestamp
  patterns replaced by :data:`TIMESTAMP_PLACEHOLDER`, and control characters
  replaced by spaces, so a description cannot smuggle a per-sample timestamp
  or split a record across lines.
* Exceptions are summarized by :func:`summarize_exception`: exception type
  names and ``file:line:function`` frames only. Exception messages, source
  lines, and frame local variables are never read or written. A
  :class:`User_Error` additionally contributes its code, description, and
  file name.
* Log files are written only inside ``<Data_Directory>/logs``
  (:data:`LOG_DIR_NAME`); :func:`configure_logging` rejects log file names
  that would resolve outside it. The handler carries
  :class:`MetadataOnlyFilter`, which drops every record not produced by a
  :class:`SafeLogger`, and :class:`MetadataFormatter`, which ignores
  ``exc_info`` / ``stack_info`` and the raw message, so plain ``logging``
  calls that reach the handler cannot leak data.

Records are written as one JSON object per line::

    {"time": "...", "level": "INFO", "component": "import",
     "operation": "import.completed", "fields": {"file_name": "...", "row_count": 3}}

This module imports only the standard library and ``backend.domain``.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import math
import os
import re
import threading
from collections.abc import Mapping
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from types import TracebackType
from typing import Any

from backend.domain.errors import ERROR_CODES, User_Error
from backend.persistence.data_directory import resolve_data_dir

__all__ = [
    "LOGGER_NAME",
    "LOG_DIR_NAME",
    "DEFAULT_LOG_FILE_NAME",
    "TIMESTAMP_PLACEHOLDER",
    "FieldKind",
    "ALLOWED_FIELDS",
    "LogFieldError",
    "MetadataOnlyFilter",
    "MetadataFormatter",
    "SafeLogger",
    "log_dir_for",
    "configure_logging",
    "shutdown_logging",
    "get_logger",
    "sanitize_text",
    "sanitize_fields",
    "summarize_exception",
]

LOGGER_NAME = "sleep_replay"
LOG_DIR_NAME = "logs"
DEFAULT_LOG_FILE_NAME = "sleep-replay.log"
TIMESTAMP_PLACEHOLDER = "<timestamp>"

# Rotation keeps the logs directory bounded; backups stay in the same directory.
_MAX_LOG_BYTES = 5 * 1024 * 1024
_BACKUP_COUNT = 3

_MAX_NAME_LEN = 255
_MAX_DESCRIPTION_LEN = 1000
_MAX_LIST_ITEMS = 100
_MAX_FRAMES = 50
_MAX_CHAIN = 5

# Attribute set on LogRecords produced by SafeLogger.
_PAYLOAD_ATTR = "sleep_replay_payload"


class FieldKind(StrEnum):
    """Validation kind of an allowed log field."""

    NAME = "name"  # file / member / metric / route name: bounded string
    NAMES = "names"  # list of NAME values
    COUNT = "count"  # non-negative int
    DURATION = "duration"  # finite non-negative int or float
    ERROR_CODE = "error_code"  # a code from the error-code table
    REF_ID = "ref_id"  # identifier-like string or non-negative int
    DESCRIPTION = "description"  # warning / error description text
    HTTP_METHOD = "http_method"  # GET, POST, ...
    HTTP_STATUS = "http_status"  # 100..599


# Operational metadata fields accepted by SafeLogger. Any other key ending in
# ``_count`` is also accepted as a COUNT.
ALLOWED_FIELDS: Mapping[str, FieldKind] = {
    # file names
    "file_name": FieldKind.NAME,
    "file_names": FieldKind.NAMES,
    "member_name": FieldKind.NAME,
    "member_names": FieldKind.NAMES,
    "source_file": FieldKind.NAME,
    "source_files": FieldKind.NAMES,
    "metric": FieldKind.NAME,
    "metrics": FieldKind.NAMES,
    "route": FieldKind.NAME,
    # counts
    "count": FieldKind.COUNT,
    # durations
    "duration_ms": FieldKind.DURATION,
    "duration_s": FieldKind.DURATION,
    # error codes
    "error_code": FieldKind.ERROR_CODE,
    # reference identifiers
    "import_id": FieldKind.REF_ID,
    "session_id": FieldKind.REF_ID,
    "replay_id": FieldKind.REF_ID,
    "request_id": FieldKind.REF_ID,
    "log_id": FieldKind.REF_ID,
    "reference_id": FieldKind.REF_ID,
    # descriptions
    "description": FieldKind.DESCRIPTION,
    "reason": FieldKind.DESCRIPTION,
    "warning_description": FieldKind.DESCRIPTION,
    "error_description": FieldKind.DESCRIPTION,
    # request logs
    "http_method": FieldKind.HTTP_METHOD,
    "http_status": FieldKind.HTTP_STATUS,
}

_HTTP_METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"})
_FIELD_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_OPERATION_RE = re.compile(r"^[a-z][a-z0-9_.:-]{0,63}$")
_REF_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_INVALID_OPERATION = "invalid_operation"

# Date-time and epoch patterns redacted from every logged string. Dates without
# a time of day (as in Fitbit file names such as ``heart_rate-2024-01-01.json``)
# are left alone.
_TIMESTAMP_PATTERNS = (
    # ISO 8601 / SQL-style date-time, optional seconds, fraction, and offset
    re.compile(
        r"\d{4}-\d{2}-\d{2}[T ]\d{1,2}:\d{2}(?::\d{2}(?:[.,]\d+)?)?"
        r"(?:\s?(?:Z|[+-]\d{2}(?::?\d{2})?))?",
        re.IGNORECASE,
    ),
    # Fitbit MM/DD/YY HH:MM:SS and similar slash dates with a time
    re.compile(
        r"\d{1,2}/\d{1,2}/\d{2,4}[ T]\d{1,2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:\s?[AP]M)?",
        re.IGNORECASE,
    ),
    # bare clock time with seconds
    re.compile(r"(?<![\d:])\d{1,2}:\d{2}:\d{2}(?:\.\d+)?(?![\d:])"),
    # Unix epoch seconds / milliseconds
    re.compile(r"(?<!\d)\d{10}(?:\d{3})?(?!\d)"),
)
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]+")


class LogFieldError(ValueError):
    """Raised in strict mode when a log call carries a non-metadata field."""


# ---------------------------------------------------------------------------
# Sanitization
# ---------------------------------------------------------------------------


def sanitize_text(text: str, max_len: int = _MAX_DESCRIPTION_LEN) -> str:
    """Return ``text`` with control characters replaced by spaces, date-time and
    epoch-timestamp patterns replaced by :data:`TIMESTAMP_PLACEHOLDER`, and the
    result truncated to ``max_len`` characters."""
    cleaned = _CONTROL_RE.sub(" ", text)
    for pattern in _TIMESTAMP_PATTERNS:
        cleaned = pattern.sub(TIMESTAMP_PLACEHOLDER, cleaned)
    cleaned = cleaned.strip()
    if len(cleaned) > max_len:
        cleaned = cleaned[: max_len - 3] + "..."
    return cleaned


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _kind_for(key: str) -> FieldKind | None:
    if not isinstance(key, str) or not _FIELD_KEY_RE.match(key):
        return None
    kind = ALLOWED_FIELDS.get(key)
    if kind is None and key.endswith("_count"):
        return FieldKind.COUNT
    return kind


_REJECT = object()


def _clean_name(value: Any) -> Any:
    if not isinstance(value, str):
        return _REJECT
    cleaned = sanitize_text(value, _MAX_NAME_LEN)
    return cleaned if cleaned else _REJECT


def _clean_value(kind: FieldKind, value: Any) -> Any:
    """Validate and normalize ``value`` for ``kind``; return ``_REJECT`` if invalid."""
    if isinstance(value, StrEnum):
        value = value.value
    if kind is FieldKind.NAME:
        return _clean_name(value)
    if kind is FieldKind.NAMES:
        if isinstance(value, (str, bytes, Mapping)) or not isinstance(value, (list, tuple, set, frozenset)):
            return _REJECT
        items = list(value)
        if isinstance(value, (set, frozenset)):
            items = sorted(items, key=str)
        cleaned = [_clean_name(v.value if isinstance(v, StrEnum) else v) for v in items[:_MAX_LIST_ITEMS]]
        if any(c is _REJECT for c in cleaned):
            return _REJECT
        return cleaned
    if kind is FieldKind.COUNT:
        return value if _is_int(value) and value >= 0 else _REJECT
    if kind is FieldKind.DURATION:
        if _is_int(value) and value >= 0:
            return value
        if isinstance(value, float) and math.isfinite(value) and value >= 0:
            return round(value, 3)
        return _REJECT
    if kind is FieldKind.ERROR_CODE:
        return value if isinstance(value, str) and value in ERROR_CODES else _REJECT
    if kind is FieldKind.REF_ID:
        if _is_int(value) and value >= 0:
            return value
        return value if isinstance(value, str) and _REF_ID_RE.match(value) else _REJECT
    if kind is FieldKind.DESCRIPTION:
        if not isinstance(value, str):
            return _REJECT
        cleaned = sanitize_text(value)
        return cleaned if cleaned else _REJECT
    if kind is FieldKind.HTTP_METHOD:
        if isinstance(value, str) and value.upper() in _HTTP_METHODS:
            return value.upper()
        return _REJECT
    if kind is FieldKind.HTTP_STATUS:
        return value if _is_int(value) and 100 <= value <= 599 else _REJECT
    return _REJECT  # pragma: no cover - exhaustive over FieldKind


def sanitize_fields(fields: Mapping[str, Any], strict: bool = False) -> tuple[dict[str, Any], int]:
    """Keep only allowed metadata fields with valid values.

    Returns:
        ``(clean_fields, rejected_count)``. Rejected fields contribute only to
        the count; their names and values are discarded.

    Raises:
        LogFieldError: In strict mode, on the first rejected field. The message
            names the field key only if it is identifier-like, and never
            includes the value.
    """
    clean: dict[str, Any] = {}
    rejected = 0
    for key, value in fields.items():
        kind = _kind_for(key)
        cleaned = _REJECT if kind is None else _clean_value(kind, value)
        if cleaned is _REJECT:
            if strict:
                shown = key if isinstance(key, str) and _FIELD_KEY_RE.match(key) else "<invalid key>"
                reason = "is not an allowed metadata field" if kind is None else f"has an invalid {kind.value} value"
                raise LogFieldError(f"log field {shown!r} {reason}")
            rejected += 1
            continue
        clean[key] = cleaned
    return clean, rejected


def summarize_exception(exc: BaseException) -> dict[str, Any]:
    """Describe ``exc`` using metadata only.

    Includes the exception type name, ``file:line:function`` frames (file base
    names, innermost last), and the type names of chained causes. For a
    :class:`User_Error` it also includes the code, the sanitized description,
    and the file name. Exception messages, source lines, and frame local
    variables are never read.
    """
    summary: dict[str, Any] = {"type": type(exc).__name__}
    if isinstance(exc, User_Error):
        summary["error_code"] = exc.code
        summary["description"] = sanitize_text(exc.description)
        if exc.file_name:
            summary["file_name"] = sanitize_text(exc.file_name, _MAX_NAME_LEN)
    summary["frames"] = _frames(exc.__traceback__)

    chain: list[str] = []
    seen = {id(exc)}
    current: BaseException | None = exc
    while current is not None and len(chain) < _MAX_CHAIN:
        nxt = current.__cause__ if current.__cause__ is not None else (
            None if current.__suppress_context__ else current.__context__
        )
        if nxt is None or id(nxt) in seen:
            break
        seen.add(id(nxt))
        chain.append(type(nxt).__name__)
        current = nxt
    if chain:
        summary["caused_by"] = chain
    return summary


def _frames(tb: TracebackType | None) -> list[str]:
    # Walk the traceback directly: only code-object metadata and line numbers
    # are touched; f_locals and linecache (source text) are never accessed.
    frames: list[str] = []
    while tb is not None:
        code = tb.tb_frame.f_code
        frames.append(f"{os.path.basename(code.co_filename)}:{tb.tb_lineno}:{code.co_name}")
        tb = tb.tb_next
    return frames[-_MAX_FRAMES:]


# ---------------------------------------------------------------------------
# Handler plumbing
# ---------------------------------------------------------------------------


class MetadataOnlyFilter(logging.Filter):
    """Pass only records produced by :class:`SafeLogger`.

    Attach it to any handler (for example request-log handlers) that must not
    receive free-form log records.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        return isinstance(getattr(record, _PAYLOAD_ATTR, None), dict)


class MetadataFormatter(logging.Formatter):
    """Format SafeLogger payloads as one JSON object per line.

    The raw message, ``args``, ``exc_info``, and ``stack_info`` of the record
    are ignored, so standard tracebacks (which may show local values in
    messages) never reach the file.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload = getattr(record, _PAYLOAD_ATTR, None)
        if not isinstance(payload, dict):
            payload = {"operation": _INVALID_OPERATION, "fields": {}}
        entry = {
            "time": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            **payload,
        }
        return json.dumps(entry, ensure_ascii=False, separators=(",", ":"))

    def formatException(self, ei: Any) -> str:  # noqa: N802 - stdlib override
        return ""

    def formatStack(self, stack_info: str) -> str:  # noqa: N802 - stdlib override
        return ""


_lock = threading.Lock()
_handler: logging.Handler | None = None
_strict = False

_base_logger = logging.getLogger(LOGGER_NAME)
_base_logger.propagate = False  # never hand records to root / third-party handlers
_base_logger.addHandler(logging.NullHandler())  # silent until configured


def log_dir_for(data_dir: Path) -> Path:
    """Return the log directory inside ``data_dir``."""
    return Path(os.path.abspath(data_dir)) / LOG_DIR_NAME


def configure_logging(
    data_dir: Path | None = None,
    *,
    level: int = logging.INFO,
    file_name: str = DEFAULT_LOG_FILE_NAME,
    strict: bool = False,
) -> Path:
    """Send SafeLogger output to ``<data_dir>/logs/<file_name>``.

    Replaces any handler installed by an earlier call. Call after
    ``prepare_data_dir`` at startup.

    Args:
        data_dir: The Data_Directory; defaults to ``resolve_data_dir()``.
        level: Minimum level written.
        file_name: Log file base name. Must be a plain file name that stays
            inside the log directory.
        strict: Raise :class:`LogFieldError` on non-metadata fields instead of
            dropping them (useful in tests and development).

    Returns:
        The absolute log file path.

    Raises:
        ValueError: If ``file_name`` is not a plain file name inside the log directory.
        OSError: If the log directory or file cannot be created.
    """
    global _handler, _strict
    base = resolve_data_dir() if data_dir is None else Path(data_dir)
    logs = log_dir_for(base)
    if (
        not file_name
        or file_name in {".", ".."}
        or os.path.basename(file_name) != file_name
        or "/" in file_name
        or "\\" in file_name
    ):
        raise ValueError("log file name must be a plain file name")
    logs.mkdir(parents=True, exist_ok=True)
    path = (logs / file_name).resolve()
    if path.parent != logs.resolve():
        raise ValueError("log file must be inside the Data_Directory log directory")

    handler = logging.handlers.RotatingFileHandler(
        path, maxBytes=_MAX_LOG_BYTES, backupCount=_BACKUP_COUNT, encoding="utf-8"
    )
    handler.addFilter(MetadataOnlyFilter())
    handler.setFormatter(MetadataFormatter())
    with _lock:
        _remove_handler_locked()
        _handler = handler
        _strict = strict
        _base_logger.addHandler(handler)
        _base_logger.setLevel(level)
    return path


def shutdown_logging() -> None:
    """Remove and close the file handler installed by :func:`configure_logging`."""
    global _strict
    with _lock:
        _remove_handler_locked()
        _strict = False


def _remove_handler_locked() -> None:
    global _handler
    if _handler is not None:
        _base_logger.removeHandler(_handler)
        _handler.close()
        _handler = None


# ---------------------------------------------------------------------------
# SafeLogger
# ---------------------------------------------------------------------------


class SafeLogger:
    """Logger that accepts only an operation name and metadata fields.

    Example::

        log = get_logger("import")
        log.info("import.completed", file_name="heart_rate-2024-01-01.json",
                 row_count=1440, duration_ms=12.5, import_id="imp-7")
        try:
            ...
        except Exception as exc:
            log.exception("import.failed", exc, file_name="data.csv")
            raise
    """

    def __init__(self, component: str, strict: bool | None = None) -> None:
        self._component = component if _OPERATION_RE.match(component or "") else _INVALID_OPERATION
        self._strict = strict
        self._logger = logging.getLogger(f"{LOGGER_NAME}.{self._component}")

    @property
    def component(self) -> str:
        return self._component

    def _is_strict(self) -> bool:
        return _strict if self._strict is None else self._strict

    def log(self, level: int, operation: str, /, **fields: Any) -> None:
        """Write a record at ``level`` for ``operation`` with metadata ``fields``."""
        self._emit(level, operation, fields, None)

    def debug(self, operation: str, /, **fields: Any) -> None:
        self._emit(logging.DEBUG, operation, fields, None)

    def info(self, operation: str, /, **fields: Any) -> None:
        self._emit(logging.INFO, operation, fields, None)

    def warning(self, operation: str, /, **fields: Any) -> None:
        self._emit(logging.WARNING, operation, fields, None)

    def error(self, operation: str, /, **fields: Any) -> None:
        self._emit(logging.ERROR, operation, fields, None)

    def exception(
        self, operation: str, exc: BaseException, /, *, level: int = logging.ERROR, **fields: Any
    ) -> None:
        """Log ``exc`` as a metadata-only summary (see :func:`summarize_exception`)."""
        self._emit(level, operation, fields, exc)

    def _emit(self, level: int, operation: str, fields: Mapping[str, Any], exc: BaseException | None) -> None:
        if not self._logger.isEnabledFor(level):
            return
        strict = self._is_strict()
        if not (isinstance(operation, str) and _OPERATION_RE.match(operation)):
            if strict:
                raise LogFieldError("operation must be a short lowercase identifier")
            operation = _INVALID_OPERATION
        clean, rejected = sanitize_fields(fields, strict=strict)
        payload: dict[str, Any] = {"component": self._component, "operation": operation, "fields": clean}
        if rejected:
            payload["rejected_field_count"] = rejected
        if exc is not None:
            payload["exception"] = summarize_exception(exc)
        message = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        # The sanitized payload is also the record message, so any handler that
        # formats msg directly still sees metadata only. No args / exc_info.
        self._logger.log(level, "%s", message, extra={_PAYLOAD_ATTR: payload})


def get_logger(component: str, strict: bool | None = None) -> SafeLogger:
    """Return a :class:`SafeLogger` for ``component`` (for example ``"import"``).

    ``strict=None`` follows the setting passed to :func:`configure_logging`.
    """
    return SafeLogger(component, strict=strict)
