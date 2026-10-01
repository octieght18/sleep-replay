"""SQLite Metadata_Store (Requirement 15.1, 16.5).

The Metadata_Store is one SQLite database file,
``<Data_Directory>/metadata.sqlite3``, holding records and metadata only:

- ``imports``: one row per completed import: id, Source_Identifier, the
  Data_Directory-relative telemetry file reference (see
  :mod:`backend.persistence.telemetry_store`), the Import_Report as JSON, and
  the creation time.
- ``import_files``: the import's source file names in order, each with
  optional per-file metadata (for example the Fitbit file-name date, stored in
  its own ``file_date`` column so it can be queried).
- ``sessions``: the candidate SleepSessions an import produced (bounds,
  provenance, and Stage_Segments; never TelemetryPoints).
- ``session_selection``: at most one selected SleepSession and how it was
  selected (``auto``, ``user``, or ``manual``). The row is self-contained, so
  a merged or manually defined session needs no candidate row.
- ``settings``: key -> JSON value (for example Mapping_Config values).
- ``replays``: Replay records (bounds, Target_Duration, WAV file reference,
  JSON metadata).

Telemetry values never enter the database: they live in the per-import files
written by :class:`backend.persistence.telemetry_store.TelemetryStore`. A
SleepSession's ``telemetry`` tuple is dropped when it is stored.

Transactions (Requirement 16.5): every public write runs in a transaction, and
callers can group several writes with :meth:`MetadataStore.transaction`
(nestable through savepoints). If anything inside fails, every change of that
transaction is rolled back, so rows stored before the operation stay
unchanged. SQLite failures surface as a ``SAVE_FAILED`` User_Error; a
database or row that cannot be read surfaces as ``PERSISTED_DATA_UNREADABLE``.
Invalid arguments (a programming error) raise ``ValueError`` before anything
is written.

Connections: a store owns one connection, opened in the constructor and
closed by :meth:`MetadataStore.close` (or leaving a ``with`` block), so the
database file can be deleted right afterwards, including on Windows. The
store is safe to share between threads; a transaction holds the store's lock
for its whole duration.

Nothing in this module logs, and errors carry only ids, file names and paths
(Requirement 15.3).

This module imports only the standard library and ``backend.domain``.
"""

from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import threading
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Literal, get_args

from backend.domain.errors import PERSISTED_DATA_UNREADABLE, SAVE_FAILED, Import_Report, User_Error
from backend.domain.session import SleepSession
from backend.domain.stages import Sleep_Stage, Stage_Segment

__all__ = [
    "METADATA_DB_FILENAME",
    "SCHEMA_VERSION",
    "FILE_DATE_KEY",
    "SelectionKind",
    "SELECTION_KINDS",
    "FileRecord",
    "ImportRecord",
    "SelectedSession",
    "ReplayRecord",
    "MetadataStore",
    "validate_record_id",
    "validate_relative_path",
]

#: File name of the database inside the Data_Directory.
METADATA_DB_FILENAME = "metadata.sqlite3"
#: Stored in ``PRAGMA user_version``; a newer database is refused as unreadable.
SCHEMA_VERSION = 2
#: Key under which :attr:`ImportRecord.file_metadata` exposes a file's date.
FILE_DATE_KEY = "file_date"

SelectionKind = Literal["auto", "user", "manual"]
SELECTION_KINDS: frozenset[str] = frozenset(get_args(SelectionKind))

# Same rule as telemetry_store import ids: ids may become file names.
_RECORD_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")

_SCHEMA: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS import_context (
        import_id TEXT PRIMARY KEY REFERENCES imports(import_id) ON DELETE CASCADE,
        context TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS imports (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        import_id TEXT NOT NULL UNIQUE,
        source_identifier TEXT NOT NULL,
        telemetry_file TEXT,
        import_report TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS import_files (
        import_id TEXT NOT NULL REFERENCES imports(import_id) ON DELETE CASCADE,
        position INTEGER NOT NULL,
        file_name TEXT NOT NULL,
        file_date TEXT,
        metadata TEXT NOT NULL,
        PRIMARY KEY (import_id, position)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sessions (
        import_id TEXT NOT NULL REFERENCES imports(import_id) ON DELETE CASCADE,
        position INTEGER NOT NULL,
        source TEXT NOT NULL,
        start_time TEXT NOT NULL,
        end_time TEXT NOT NULL,
        log_id TEXT,
        is_main_sleep INTEGER NOT NULL CHECK (is_main_sleep IN (0, 1)),
        session_hrv REAL,
        source_file TEXT,
        stages TEXT NOT NULL,
        PRIMARY KEY (import_id, position)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS session_selection (
        slot INTEGER PRIMARY KEY CHECK (slot = 1),
        kind TEXT NOT NULL CHECK (kind IN ('auto', 'user', 'manual')),
        source TEXT NOT NULL,
        start_time TEXT NOT NULL,
        end_time TEXT NOT NULL,
        log_id TEXT,
        is_main_sleep INTEGER NOT NULL CHECK (is_main_sleep IN (0, 1)),
        session_hrv REAL,
        source_file TEXT,
        stages TEXT NOT NULL,
        selected_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS replays (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        replay_id TEXT NOT NULL UNIQUE,
        session_start TEXT NOT NULL,
        session_end TEXT NOT NULL,
        target_duration_s REAL NOT NULL,
        wav_file TEXT NOT NULL,
        metadata TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
)

_SESSION_COLUMNS = "source, start_time, end_time, log_id, is_main_sleep, session_hrv, source_file, stages"


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FileRecord:
    """One source file of an import.

    Attributes:
        name: File (or archive member) name as reported to the user.
        file_date: The date in the file name (Fitbit daily/monthly files), if any.
        metadata: Other per-file metadata: JSON-compatible values
            (``str``, ``int``, finite ``float``, ``bool``, ``None``, lists,
            string-keyed mappings); ``date``/``datetime`` become ISO strings.
            Never telemetry values.
    """

    name: str
    file_date: date | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ImportRecord:
    """A stored import (read back from the Metadata_Store)."""

    import_id: str
    source_identifier: str
    files: tuple[FileRecord, ...]
    telemetry_file: str | None
    import_report: Import_Report
    created_at: datetime

    @property
    def source_files(self) -> list[str]:
        """Source file names in the order they were recorded."""
        return [f.name for f in self.files]

    @property
    def file_dates(self) -> dict[str, date]:
        """File name -> file-name date, for files that have one."""
        return {f.name: f.file_date for f in self.files if f.file_date is not None}

    @property
    def file_metadata(self) -> dict[str, dict[str, Any]]:
        """File name -> metadata, with the file date (ISO string) under :data:`FILE_DATE_KEY`."""
        result: dict[str, dict[str, Any]] = {}
        for f in self.files:
            entry = dict(f.metadata)
            if f.file_date is not None:
                entry[FILE_DATE_KEY] = f.file_date.isoformat()
            result[f.name] = entry
        return result


@dataclass(frozen=True)
class SelectedSession:
    """The selected SleepSession and how it became selected (Requirement 8)."""

    session: SleepSession
    kind: SelectionKind
    selected_at: datetime


@dataclass(frozen=True)
class ReplayRecord:
    """A stored Replay record."""

    replay_id: str
    session_start: datetime
    session_end: datetime
    target_duration_s: float
    wav_file: str
    metadata: dict[str, Any]
    created_at: datetime


# ---------------------------------------------------------------------------
# Validation and encoding helpers
# ---------------------------------------------------------------------------


class _CorruptRow(Exception):
    """Internal: a stored row cannot be decoded."""


def validate_record_id(value: str, what: str = "id") -> str:
    """Return ``value`` if it is 1-128 letters, digits, '_' or '-' (starting alphanumeric)."""
    if not isinstance(value, str) or not _RECORD_ID_RE.fullmatch(value):
        raise ValueError(f"{what} must be 1-128 characters of letters, digits, '_' or '-'")
    return value


def validate_relative_path(value: str, what: str = "path") -> str:
    """Return ``value`` if it is a Data_Directory-relative POSIX path that stays inside it."""
    if not isinstance(value, str) or not value or "\\" in value or ":" in value or "\x00" in value:
        raise ValueError(f"{what} must be a non-empty relative POSIX path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in ("", ".", "..") for part in value.split("/")):
        raise ValueError(f"{what} must stay inside the Data_Directory")
    return value


def _require_aware(value: datetime, what: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{what} must be a timezone-aware datetime")
    return value


def _to_json_compatible(value: Any, what: str) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{what} contains a non-finite number")
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{what} keys must be strings")
            out[key] = _to_json_compatible(item, what)
        return out
    if isinstance(value, (list, tuple)):
        return [_to_json_compatible(item, what) for item in value]
    raise ValueError(f"{what} contains a value that is not JSON-compatible")


def _dumps(value: Any, what: str) -> str:
    return json.dumps(_to_json_compatible(value, what), ensure_ascii=True, allow_nan=False, separators=(",", ":"))


def _loads(text: Any) -> Any:
    if not isinstance(text, str):
        raise _CorruptRow("expected JSON text")
    try:
        return json.loads(text)
    except (json.JSONDecodeError, RecursionError):
        raise _CorruptRow("invalid JSON") from None


def _parse_datetime(text: Any) -> datetime:
    if not isinstance(text, str):
        raise _CorruptRow("expected a timestamp")
    try:
        value = datetime.fromisoformat(text)
    except ValueError:
        raise _CorruptRow("invalid timestamp") from None
    if value.tzinfo is None:
        raise _CorruptRow("naive timestamp")
    return value


def _parse_date(text: Any) -> date | None:
    if text is None:
        return None
    if not isinstance(text, str):
        raise _CorruptRow("expected a date")
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise _CorruptRow("invalid date") from None


def _coerce_file(item: str | FileRecord) -> FileRecord:
    if isinstance(item, str):
        item = FileRecord(item)
    if not isinstance(item, FileRecord) or not isinstance(item.name, str) or not item.name:
        raise ValueError("each source file must be a non-empty name or a FileRecord")
    if item.file_date is not None and (not isinstance(item.file_date, date) or isinstance(item.file_date, datetime)):
        raise ValueError("FileRecord.file_date must be a date")
    return item


def _session_values(session: SleepSession) -> tuple[Any, ...]:
    """Column values for a SleepSession (``telemetry`` is deliberately dropped)."""
    if not isinstance(session, SleepSession):
        raise ValueError("expected a SleepSession")
    _require_aware(session.start_time, "SleepSession.start_time")
    _require_aware(session.end_time, "SleepSession.end_time")
    if not isinstance(session.source, str) or not session.source:
        raise ValueError("SleepSession.source must be a non-empty string")
    hrv = session.session_hrv
    if hrv is not None and (isinstance(hrv, bool) or not isinstance(hrv, (int, float)) or not math.isfinite(hrv)):
        raise ValueError("SleepSession.session_hrv must be a finite number or None")
    stages = [
        {
            "start": seg.start_time.isoformat(),
            "end": seg.end_time.isoformat(),
            "stage": Sleep_Stage(seg.stage).value,
            "brief": bool(seg.is_brief_awakening),
        }
        for seg in session.stages
    ]
    return (
        session.source,
        session.start_time.isoformat(),
        session.end_time.isoformat(),
        None if session.log_id is None else str(session.log_id),
        1 if session.is_main_sleep else 0,
        None if hrv is None else float(hrv),
        None if session.source_file is None else str(session.source_file),
        json.dumps(stages, ensure_ascii=True, separators=(",", ":")),
    )


def _session_from_row(row: sqlite3.Row) -> SleepSession:
    raw_stages = _loads(row["stages"])
    if not isinstance(raw_stages, list):
        raise _CorruptRow("stages is not a list")
    stages = []
    for raw in raw_stages:
        if not isinstance(raw, dict):
            raise _CorruptRow("malformed stage segment")
        try:
            stages.append(
                Stage_Segment(
                    start_time=_parse_datetime(raw.get("start")),
                    end_time=_parse_datetime(raw.get("end")),
                    stage=Sleep_Stage(raw.get("stage")),
                    is_brief_awakening=bool(raw.get("brief", False)),
                )
            )
        except (ValueError, TypeError):
            raise _CorruptRow("invalid stage segment") from None
    source = row["source"]
    if not isinstance(source, str):
        raise _CorruptRow("invalid source")
    hrv = row["session_hrv"]
    if hrv is not None and not isinstance(hrv, (int, float)):
        raise _CorruptRow("invalid session_hrv")
    return SleepSession(
        start_time=_parse_datetime(row["start_time"]),
        end_time=_parse_datetime(row["end_time"]),
        source=source,
        stages=tuple(stages),
        log_id=row["log_id"],
        is_main_sleep=bool(row["is_main_sleep"]),
        session_hrv=None if hrv is None else float(hrv),
        source_file=row["source_file"],
    )


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class MetadataStore:
    """The SQLite Metadata_Store inside the Data_Directory.

    Args:
        data_dir: The Data_Directory (already prepared by
            :func:`backend.persistence.data_directory.prepare_data_dir`).
        clock: Returns the current aware time for ``created_at`` /
            ``selected_at`` (defaults to UTC now; injectable for tests).

    Raises:
        User_Error: ``PERSISTED_DATA_UNREADABLE`` if the database file exists
            but is not a readable Metadata_Store; ``SAVE_FAILED`` if it
            cannot be created.

    Use as a context manager, or call :meth:`close`, to release the file.
    """

    def __init__(self, data_dir: str | os.PathLike[str], clock: Callable[[], datetime] | None = None) -> None:
        self._data_dir = Path(data_dir)
        self._path = self._data_dir / METADATA_DB_FILENAME
        self._clock = clock if clock is not None else _utc_now
        self._lock = threading.RLock()
        self._depth = 0
        self._conn: sqlite3.Connection | None = None
        self._open()

    # -- lifecycle ------------------------------------------------------------

    @property
    def data_dir(self) -> Path:
        return self._data_dir

    @property
    def path(self) -> Path:
        """Absolute path of the database file."""
        return self._path

    @property
    def closed(self) -> bool:
        return self._conn is None

    def _open(self) -> None:
        conn: sqlite3.Connection | None = None
        try:
            # isolation_level=None: no implicit transactions; we issue BEGIN/COMMIT ourselves.
            conn = sqlite3.connect(str(self._path), isolation_level=None, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise _CorruptRow("database was written by a newer version")
            conn.execute("BEGIN IMMEDIATE")
            try:
                for statement in _SCHEMA:
                    conn.execute(statement)
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        except (sqlite3.Error, _CorruptRow) as exc:
            if conn is not None:
                conn.close()
            if isinstance(exc, sqlite3.OperationalError) and not self._path.is_file():
                raise self._save_failed() from None
            raise self._unreadable() from None
        self._conn = conn

    def close(self) -> None:
        """Close the connection (idempotent). Rolls back an open transaction."""
        with self._lock:
            conn, self._conn = self._conn, None
            if conn is not None:
                try:
                    if conn.in_transaction:
                        conn.execute("ROLLBACK")
                finally:
                    conn.close()
                self._depth = 0

    def __enter__(self) -> MetadataStore:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _connection(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("MetadataStore is closed")
        return self._conn

    # -- transactions ---------------------------------------------------------

    @contextmanager
    def transaction(self) -> Iterator[MetadataStore]:
        """Group writes atomically (Requirement 16.5).

        Nested use creates a savepoint: an inner failure rolls back only the
        inner block (the exception still propagates to the outer block unless
        caught). Any exception rolls back the block's changes and is
        re-raised; SQLite errors are re-raised as a ``SAVE_FAILED`` User_Error.
        """
        with self._lock:
            conn = self._connection()
            depth = self._depth
            savepoint = f"sp_{depth}"
            try:
                conn.execute("BEGIN IMMEDIATE" if depth == 0 else f"SAVEPOINT {savepoint}")
            except sqlite3.Error:
                raise self._save_failed() from None
            self._depth = depth + 1
            try:
                yield self
            except BaseException as exc:
                self._depth = depth
                self._rollback(conn, depth, savepoint)
                if isinstance(exc, sqlite3.Error):
                    raise self._save_failed() from None
                raise
            self._depth = depth
            try:
                conn.execute("COMMIT" if depth == 0 else f"RELEASE SAVEPOINT {savepoint}")
            except sqlite3.Error:
                self._rollback(conn, depth, savepoint)
                raise self._save_failed() from None

    @staticmethod
    def _rollback(conn: sqlite3.Connection, depth: int, savepoint: str) -> None:
        try:
            if depth == 0:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
            else:
                conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        except sqlite3.Error:
            pass

    @contextmanager
    def _reading(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            conn = self._connection()
            try:
                yield conn
            except (sqlite3.Error, _CorruptRow):
                raise self._unreadable() from None

    # -- imports ----------------------------------------------------------------

    def record_import(
        self,
        import_id: str,
        source_identifier: str,
        source_files: Sequence[str | FileRecord],
        *,
        telemetry_file: str | None = None,
        import_report: Import_Report | None = None,
        sessions: Iterable[SleepSession] = (),
        context: Mapping[str, Any] | None = None,
        replace: bool = False,
    ) -> ImportRecord:
        """Store an import record, its files, and its candidate sessions in one transaction.

        Args:
            import_id: Record id (same rule as telemetry file ids).
            source_identifier: The adapter's Source_Identifier (e.g. ``fitbit``).
            source_files: File names in order, or :class:`FileRecord` entries
                carrying per-file metadata such as the file-name date.
            telemetry_file: Data_Directory-relative telemetry file reference,
                e.g. ``TelemetryStore.relative_path(import_id)``.
            import_report: The import's Import_Report (no telemetry values).
            sessions: Candidate SleepSessions from this import; their
                ``telemetry`` is not stored.
            replace: Replace an existing record with the same id (its files
                and sessions too) instead of failing.

        Returns:
            The stored record as it reads back.

        Raises:
            ValueError: Invalid arguments, or ``import_id`` already recorded
                and ``replace`` is False. Nothing is written.
            User_Error: ``SAVE_FAILED``; prior rows are unchanged.
        """
        validate_record_id(import_id, "import_id")
        if not isinstance(source_identifier, str) or not source_identifier:
            raise ValueError("source_identifier must be a non-empty string")
        files = [_coerce_file(item) for item in source_files]
        file_rows = [
            (
                import_id,
                position,
                f.name,
                None if f.file_date is None else f.file_date.isoformat(),
                _dumps(dict(f.metadata), "FileRecord.metadata"),
            )
            for position, f in enumerate(files)
        ]
        if telemetry_file is not None:
            validate_relative_path(telemetry_file, "telemetry_file")
        report = import_report if import_report is not None else Import_Report()
        if not isinstance(report, Import_Report):
            raise ValueError("import_report must be an Import_Report")
        report_json = _dumps(report.to_dict(), "import_report")
        session_rows = [(import_id, position, *_session_values(s)) for position, s in enumerate(sessions)]
        created_at = _require_aware(self._clock(), "clock()").isoformat()

        with self.transaction() as _:
            conn = self._connection()
            exists = conn.execute("SELECT 1 FROM imports WHERE import_id = ?", (import_id,)).fetchone()
            if exists is not None:
                if not replace:
                    raise ValueError(f"import {import_id} is already recorded")
                conn.execute("DELETE FROM imports WHERE import_id = ?", (import_id,))
            conn.execute(
                "INSERT INTO imports (import_id, source_identifier, telemetry_file, import_report, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (import_id, source_identifier, telemetry_file, report_json, created_at),
            )
            conn.executemany(
                "INSERT INTO import_files (import_id, position, file_name, file_date, metadata) VALUES (?, ?, ?, ?, ?)",
                file_rows,
            )
            conn.executemany(
                f"INSERT INTO sessions (import_id, position, {_SESSION_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                session_rows,
            )
            conn.execute("INSERT INTO import_context (import_id, context) VALUES (?, ?)",
                         (import_id, _dumps(dict(context or {}), "import context")))
            record = self.get_import(import_id)
        assert record is not None
        return record

    def update_import_report(self, import_id: str, import_report: Import_Report) -> None:
        """Replace an import's Import_Report (for example after session discovery).

        Raises:
            KeyError: No such import.
            User_Error: ``SAVE_FAILED``; the previous report is kept.
        """
        validate_record_id(import_id, "import_id")
        report_json = _dumps(import_report.to_dict(), "import_report")
        with self.transaction():
            cur = self._connection().execute(
                "UPDATE imports SET import_report = ? WHERE import_id = ?", (report_json, import_id)
            )
            if cur.rowcount == 0:
                raise KeyError(import_id)

    def set_candidate_sessions(self, import_id: str, sessions: Iterable[SleepSession]) -> None:
        """Replace the candidate sessions stored for an import.

        Raises:
            KeyError: No such import.
            User_Error: ``SAVE_FAILED``; the previous sessions are kept.
        """
        validate_record_id(import_id, "import_id")
        rows = [(import_id, position, *_session_values(s)) for position, s in enumerate(sessions)]
        with self.transaction():
            conn = self._connection()
            if conn.execute("SELECT 1 FROM imports WHERE import_id = ?", (import_id,)).fetchone() is None:
                raise KeyError(import_id)
            conn.execute("DELETE FROM sessions WHERE import_id = ?", (import_id,))
            conn.executemany(
                f"INSERT INTO sessions (import_id, position, {_SESSION_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )

    def delete_import(self, import_id: str) -> bool:
        """Delete an import record with its files and candidate sessions.

        Does not touch the telemetry file or the session selection. Returns
        False if the import did not exist.
        """
        validate_record_id(import_id, "import_id")
        with self.transaction():
            cur = self._connection().execute("DELETE FROM imports WHERE import_id = ?", (import_id,))
            return cur.rowcount > 0

    def has_import(self, import_id: str) -> bool:
        validate_record_id(import_id, "import_id")
        with self._reading() as conn:
            return conn.execute("SELECT 1 FROM imports WHERE import_id = ?", (import_id,)).fetchone() is not None

    def get_import(self, import_id: str) -> ImportRecord | None:
        """The stored import, or None.

        Raises:
            User_Error: ``PERSISTED_DATA_UNREADABLE`` if the record is damaged.
        """
        validate_record_id(import_id, "import_id")
        with self._reading() as conn:
            row = conn.execute("SELECT * FROM imports WHERE import_id = ?", (import_id,)).fetchone()
            return None if row is None else self._import_from_row(conn, row)

    def list_imports(self) -> list[ImportRecord]:
        """All stored imports in the order they were recorded."""
        with self._reading() as conn:
            rows = conn.execute("SELECT * FROM imports ORDER BY seq").fetchall()
            return [self._import_from_row(conn, row) for row in rows]

    def list_import_ids(self) -> list[str]:
        with self._reading() as conn:
            return [row[0] for row in conn.execute("SELECT import_id FROM imports ORDER BY seq")]

    def get_import_context(self, import_id: str) -> dict[str, Any]:
        """Restart-safe provenance, daily HRV rows, and timezone inputs for an import."""
        validate_record_id(import_id, "import_id")
        with self._reading() as conn:
            row = conn.execute("SELECT context FROM import_context WHERE import_id = ?", (import_id,)).fetchone()
            context = {} if row is None else _loads(row[0])
            if not isinstance(context, dict):
                raise _CorruptRow("invalid import context")
            return context

    def list_candidate_sessions(self, import_id: str | None = None) -> list[tuple[str, SleepSession]]:
        """``(import_id, session)`` pairs, in import order then stored order.

        Sessions come back without telemetry.
        """
        if import_id is not None:
            validate_record_id(import_id, "import_id")
        query = (
            f"SELECT s.import_id, {', '.join('s.' + c.strip() for c in _SESSION_COLUMNS.split(','))}"
            " FROM sessions s JOIN imports i ON i.import_id = s.import_id"
        )
        params: tuple[Any, ...] = ()
        if import_id is not None:
            query += " WHERE s.import_id = ?"
            params = (import_id,)
        query += " ORDER BY i.seq, s.position"
        with self._reading() as conn:
            return [(row["import_id"], _session_from_row(row)) for row in conn.execute(query, params)]

    def _import_from_row(self, conn: sqlite3.Connection, row: sqlite3.Row) -> ImportRecord:
        files = []
        for frow in conn.execute(
            "SELECT file_name, file_date, metadata FROM import_files WHERE import_id = ? ORDER BY position",
            (row["import_id"],),
        ):
            metadata = _loads(frow["metadata"])
            if not isinstance(metadata, dict):
                raise _CorruptRow("file metadata is not an object")
            files.append(FileRecord(frow["file_name"], _parse_date(frow["file_date"]), metadata))
        report_data = _loads(row["import_report"])
        if not isinstance(report_data, dict):
            raise _CorruptRow("import report is not an object")
        try:
            report = Import_Report.from_dict(report_data)
        except (KeyError, TypeError, ValueError):
            raise _CorruptRow("invalid import report") from None
        return ImportRecord(
            import_id=row["import_id"],
            source_identifier=row["source_identifier"],
            files=tuple(files),
            telemetry_file=row["telemetry_file"],
            import_report=report,
            created_at=_parse_datetime(row["created_at"]),
        )

    # -- session selection -----------------------------------------------------

    def set_selected_session(self, session: SleepSession, kind: SelectionKind) -> SelectedSession:
        """Make ``session`` the selected SleepSession, replacing any previous selection."""
        if kind not in SELECTION_KINDS:
            raise ValueError(f"kind must be one of {sorted(SELECTION_KINDS)}")
        values = _session_values(session)
        selected_at = _require_aware(self._clock(), "clock()")
        with self.transaction():
            self._connection().execute(
                f"INSERT OR REPLACE INTO session_selection (slot, kind, {_SESSION_COLUMNS}, selected_at)"
                " VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (kind, *values, selected_at.isoformat()),
            )
        return SelectedSession(session=_strip_telemetry(session), kind=kind, selected_at=selected_at)

    def get_selected_session(self) -> SelectedSession | None:
        """The selected SleepSession (without telemetry), or None."""
        with self._reading() as conn:
            row = conn.execute("SELECT * FROM session_selection WHERE slot = 1").fetchone()
            if row is None:
                return None
            if row["kind"] not in SELECTION_KINDS:
                raise _CorruptRow("invalid selection kind")
            return SelectedSession(
                session=_session_from_row(row), kind=row["kind"], selected_at=_parse_datetime(row["selected_at"])
            )

    def clear_selected_session(self) -> bool:
        """Remove the selection. Returns False if nothing was selected."""
        with self.transaction():
            return self._connection().execute("DELETE FROM session_selection").rowcount > 0

    # -- settings -------------------------------------------------------------------

    def set_setting(self, key: str, value: Any) -> None:
        """Store a JSON-compatible setting value."""
        self.set_settings({key: value})

    def set_settings(self, values: Mapping[str, Any]) -> None:
        """Store several settings atomically."""
        rows = []
        for key, value in values.items():
            if not isinstance(key, str) or not key:
                raise ValueError("setting keys must be non-empty strings")
            rows.append((key, _dumps(value, f"setting {key}")))
        with self.transaction():
            self._connection().executemany("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", rows)

    def get_setting(self, key: str, default: Any = None) -> Any:
        with self._reading() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
            return default if row is None else _loads(row["value"])

    def get_settings(self) -> dict[str, Any]:
        with self._reading() as conn:
            return {row["key"]: _loads(row["value"]) for row in conn.execute("SELECT key, value FROM settings ORDER BY key")}

    def delete_setting(self, key: str) -> bool:
        with self.transaction():
            return self._connection().execute("DELETE FROM settings WHERE key = ?", (key,)).rowcount > 0

    # -- replays ----------------------------------------------------------------------

    def record_replay(
        self,
        replay_id: str,
        *,
        session_start: datetime,
        session_end: datetime,
        target_duration_s: float,
        wav_file: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> ReplayRecord:
        """Store a Replay record. Call only after the WAV file is complete.

        Raises:
            ValueError: Invalid arguments or a duplicate ``replay_id``.
            User_Error: ``SAVE_FAILED``; no record is kept.
        """
        validate_record_id(replay_id, "replay_id")
        _require_aware(session_start, "session_start")
        _require_aware(session_end, "session_end")
        if isinstance(target_duration_s, bool) or not isinstance(target_duration_s, (int, float)):
            raise ValueError("target_duration_s must be a number")
        if not math.isfinite(target_duration_s) or target_duration_s <= 0:
            raise ValueError("target_duration_s must be positive and finite")
        validate_relative_path(wav_file, "wav_file")
        metadata_json = _dumps(dict(metadata or {}), "replay metadata")
        created_at = _require_aware(self._clock(), "clock()").isoformat()
        with self.transaction():
            conn = self._connection()
            if conn.execute("SELECT 1 FROM replays WHERE replay_id = ?", (replay_id,)).fetchone() is not None:
                raise ValueError(f"replay {replay_id} is already recorded")
            conn.execute(
                "INSERT INTO replays (replay_id, session_start, session_end, target_duration_s, wav_file, metadata,"
                " created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    replay_id,
                    session_start.isoformat(),
                    session_end.isoformat(),
                    float(target_duration_s),
                    wav_file,
                    metadata_json,
                    created_at,
                ),
            )
            record = self.get_replay(replay_id)
        assert record is not None
        return record

    def get_replay(self, replay_id: str) -> ReplayRecord | None:
        validate_record_id(replay_id, "replay_id")
        with self._reading() as conn:
            row = conn.execute("SELECT * FROM replays WHERE replay_id = ?", (replay_id,)).fetchone()
            return None if row is None else _replay_from_row(row)

    def list_replays(self) -> list[ReplayRecord]:
        """All Replay records in the order they were recorded."""
        with self._reading() as conn:
            return [_replay_from_row(row) for row in conn.execute("SELECT * FROM replays ORDER BY seq")]

    def delete_replay(self, replay_id: str) -> bool:
        validate_record_id(replay_id, "replay_id")
        with self.transaction():
            return self._connection().execute("DELETE FROM replays WHERE replay_id = ?", (replay_id,)).rowcount > 0

    # -- errors ------------------------------------------------------------------------

    def _save_failed(self) -> User_Error:
        return User_Error(
            SAVE_FAILED,
            description=f"The import and session records could not be saved to the Metadata_Store in {self._data_dir}.",
            action="Make sure the Data_Directory is writable and has free disk space, then try again.",
            details={"path": str(self._path)},
        )

    def _unreadable(self) -> User_Error:
        return User_Error(
            PERSISTED_DATA_UNREADABLE,
            description=f"The Metadata_Store {self._path} could not be read or is damaged.",
            action=f"Move or delete {self._path}, then start again and re-import your files.",
            file_name=METADATA_DB_FILENAME,
            details={"path": str(self._path)},
        )


def _strip_telemetry(session: SleepSession) -> SleepSession:
    if not session.telemetry:
        return session
    return SleepSession(
        start_time=session.start_time,
        end_time=session.end_time,
        source=session.source,
        stages=session.stages,
        log_id=session.log_id,
        is_main_sleep=session.is_main_sleep,
        session_hrv=session.session_hrv,
        source_file=session.source_file,
    )


def _replay_from_row(row: sqlite3.Row) -> ReplayRecord:
    metadata = _loads(row["metadata"])
    if not isinstance(metadata, dict):
        raise _CorruptRow("replay metadata is not an object")
    duration = row["target_duration_s"]
    if not isinstance(duration, (int, float)):
        raise _CorruptRow("invalid target duration")
    return ReplayRecord(
        replay_id=row["replay_id"],
        session_start=_parse_datetime(row["session_start"]),
        session_end=_parse_datetime(row["session_end"]),
        target_duration_s=float(duration),
        wav_file=row["wav_file"],
        metadata=metadata,
        created_at=_parse_datetime(row["created_at"]),
    )
