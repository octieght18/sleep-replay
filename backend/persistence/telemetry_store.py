"""Atomic persistence of an import's normalized TelemetryPoints (Requirement 1.7-1.10, 16.5).

Each import's points live in one file, ``<Data_Directory>/telemetry/<import_id>.json``.

File format (UTF-8, ASCII-only JSON)::

    {
      "format": "sleep-replay-telemetry",
      "version": 1,
      "import_id": "<import_id>",
      "count": <number of points>,
      "points": [
        {"t": "<ISO 8601 local time with UTC offset>", "s": "<source>",
         "m": "<metric>", "u": "<unit>", "v": <value>},
        ...
      ]
    }

Round-trip exactness (Requirement 1.8):

- ``t`` is ``datetime.isoformat()`` of the original timestamp: the wall-clock
  time, including microseconds, plus the exact UTC offset in effect. Valid
  TelemetryPoints have whole-second offsets (Requirement 1.2), and every
  whole-second offset, including sub-minute ones such as ``+05:00:07``,
  round-trips exactly: ``datetime.fromisoformat`` restores the same instant and
  the same offset (as a fixed-offset ``timezone``). Offsets with a sub-second
  part are not valid and are rejected on save, because ISO 8601 text cannot
  carry them reliably (CPython reads ``+00:00:00.000001`` back as UTC).
  Compare instants with
  ``astimezone(timezone.utc)`` (or ``timestamp()``), not ``==``: per PEP 495 an
  aware datetime inside a repeated DST hour never compares equal to one in a
  different tzinfo, even at the same instant.
- ``v`` is a JSON number. ``int`` values stay integers (arbitrary size, exact);
  ``float`` values are written with ``repr`` (shortest exact representation,
  ``-0.0`` included), so the loaded value is exactly equal to the original.
- ``s``/``u`` are JSON strings with ``\\u`` escapes for non-ASCII characters,
  so any Python string (including lone surrogates) round-trips.

Atomicity (Requirement 1.7, 1.9, 16.5): :meth:`TelemetryStore.save` writes a
temporary file inside the Data_Directory, flushes and fsyncs it, then
atomically renames it over the final name with :func:`os.replace`. On any
failure the temporary file is removed and a ``SAVE_FAILED`` User_Error is
raised; other imports' files are never touched.

Loading (Requirement 1.10): :meth:`TelemetryStore.load` parses the whole file
and validates every point with :func:`backend.domain.telemetry.is_valid`
before returning any of them. An unreadable file or any invalid point raises a
``PERSISTED_DATA_UNREADABLE`` User_Error naming the import's source files; no
points are returned.

Neither errors nor anything else in this module include telemetry values or
per-sample timestamps (Requirement 15.3).

This module imports only the standard library and ``backend.domain``.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Callable, Iterable, Sequence
from datetime import datetime
from pathlib import Path
from typing import IO, Any

from backend.domain.errors import PERSISTED_DATA_UNREADABLE, SAVE_FAILED, User_Error
from backend.domain.telemetry import Metric, TelemetryPoint, is_valid

__all__ = [
    "TELEMETRY_SUBDIR",
    "FORMAT_NAME",
    "FORMAT_VERSION",
    "TEMP_PREFIX",
    "Writer",
    "default_writer",
    "serialize_points",
    "deserialize_points",
    "TelemetryStore",
]

#: Sub-directory of the Data_Directory holding the per-import telemetry files.
TELEMETRY_SUBDIR = "telemetry"
FORMAT_NAME = "sleep-replay-telemetry"
FORMAT_VERSION = 1
#: Prefix of temporary files; anything starting with it is never a finished import.
TEMP_PREFIX = ".tmp-"
_SUFFIX = ".json"
_TEMP_SUFFIX = ".json.tmp"

# Import ids become file names; restricting them keeps every write inside the
# Data_Directory (Requirement 15.1).
_IMPORT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")

#: Writes ``data`` to the open binary temp file and makes it durable.
Writer = Callable[[IO[bytes], bytes], None]


def default_writer(fh: IO[bytes], data: bytes) -> None:
    """Write ``data``, flush Python's buffer, and fsync the file."""
    fh.write(data)
    fh.flush()
    os.fsync(fh.fileno())


class _CorruptData(Exception):
    """Internal: the persisted payload is malformed or holds an invalid point."""


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


def _encode_value(value: int | float) -> int | float:
    # Normalize numeric subclasses so json emits a plain number; keep int vs float.
    if isinstance(value, int):
        return int(value)
    return float(value)


def _encode_point(point: TelemetryPoint) -> dict[str, Any]:
    return {
        "t": point.timestamp.isoformat(),
        "s": str(point.source),
        "m": Metric(point.metric).value,
        "u": str(point.unit),
        "v": _encode_value(point.value),
    }


def serialize_points(import_id: str, points: Sequence[TelemetryPoint]) -> bytes:
    """Serialize ``points`` to the persisted file format.

    Raises:
        ValueError: If any point is not a valid TelemetryPoint (a programming
            error: importers only emit points that passed the validation gate).
    """
    for index, point in enumerate(points):
        if not is_valid(point):
            raise ValueError(f"point at index {index} is not a valid TelemetryPoint")
    document = {
        "format": FORMAT_NAME,
        "version": FORMAT_VERSION,
        "import_id": import_id,
        "count": len(points),
        "points": [_encode_point(p) for p in points],
    }
    return json.dumps(document, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode("ascii")


def _reject_constant(name: str) -> Any:
    raise _CorruptData(f"non-finite constant {name}")


def _decode_point(raw: Any) -> TelemetryPoint:
    if not isinstance(raw, dict) or set(raw) != {"t", "s", "m", "u", "v"}:
        raise _CorruptData("malformed point record")
    t, s, m, u, v = raw["t"], raw["s"], raw["m"], raw["u"], raw["v"]
    if not isinstance(t, str) or not isinstance(s, str) or not isinstance(m, str) or not isinstance(u, str):
        raise _CorruptData("malformed point field")
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise _CorruptData("malformed point value")
    try:
        timestamp = datetime.fromisoformat(t)
        metric = Metric(m)
    except ValueError:
        raise _CorruptData("malformed timestamp or metric") from None
    point = TelemetryPoint(timestamp=timestamp, source=s, metric=metric, value=v, unit=u)
    if not is_valid(point):
        raise _CorruptData("invalid point")
    return point


def deserialize_points(data: bytes, expected_import_id: str | None = None) -> list[TelemetryPoint]:
    """Parse and validate a persisted file's bytes.

    Every point is validated before any is returned.

    Raises:
        ValueError: If the payload cannot be decoded, has the wrong format,
            belongs to another import, or contains an invalid point.
    """
    try:
        document = json.loads(data.decode("utf-8"), parse_constant=_reject_constant)
        if not isinstance(document, dict):
            raise _CorruptData("not an object")
        if document.get("format") != FORMAT_NAME or document.get("version") != FORMAT_VERSION:
            raise _CorruptData("unknown format")
        if expected_import_id is not None and document.get("import_id") != expected_import_id:
            raise _CorruptData("import id mismatch")
        raw_points = document.get("points")
        count = document.get("count")
        if not isinstance(raw_points, list) or isinstance(count, bool) or count != len(raw_points):
            raise _CorruptData("point list missing or truncated")
        return [_decode_point(raw) for raw in raw_points]
    except _CorruptData as exc:
        raise ValueError(str(exc)) from None
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        raise ValueError("payload is not valid JSON") from None


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class TelemetryStore:
    """Per-import telemetry files inside the Data_Directory.

    Args:
        data_dir: The Data_Directory (already prepared by
            :func:`backend.persistence.data_directory.prepare_data_dir`).
        writer: Writes bytes to the open temp file and fsyncs it. Defaults to
            :func:`default_writer`; tests inject a failing writer to simulate
            a write failure.
    """

    def __init__(self, data_dir: str | os.PathLike[str], writer: Writer | None = None) -> None:
        self._data_dir = Path(data_dir)
        self._writer: Writer = writer if writer is not None else default_writer

    # -- paths ----------------------------------------------------------------

    @property
    def data_dir(self) -> Path:
        return self._data_dir

    @property
    def telemetry_dir(self) -> Path:
        return self._data_dir / TELEMETRY_SUBDIR

    @staticmethod
    def validate_import_id(import_id: str) -> str:
        """Return ``import_id`` if it is a safe file stem, else raise ValueError."""
        if not isinstance(import_id, str) or not _IMPORT_ID_RE.fullmatch(import_id):
            raise ValueError("import_id must be 1-128 characters of letters, digits, '_' or '-'")
        return import_id

    @staticmethod
    def relative_path(import_id: str) -> str:
        """Data_Directory-relative POSIX path of the import's telemetry file."""
        TelemetryStore.validate_import_id(import_id)
        return f"{TELEMETRY_SUBDIR}/{import_id}{_SUFFIX}"

    def path_for(self, import_id: str) -> Path:
        """Absolute path of the import's telemetry file."""
        self.validate_import_id(import_id)
        return self.telemetry_dir / f"{import_id}{_SUFFIX}"

    def exists(self, import_id: str) -> bool:
        return self.path_for(import_id).is_file()

    # -- save -----------------------------------------------------------------

    def save(
        self,
        import_id: str,
        points: Sequence[TelemetryPoint],
        source_files: Iterable[str] = (),
    ) -> str:
        """Atomically persist ``points`` for ``import_id`` (Requirement 1.7, 1.9).

        Returns:
            The Data_Directory-relative path of the written file
            (see :meth:`relative_path`).

        Raises:
            ValueError: If ``import_id`` is not a safe id or a point is invalid.
            User_Error: ``SAVE_FAILED`` if the file could not be written. No
                file of this import is left behind (a previously saved file
                with the same id is left as it was), and other imports' files
                are untouched.
        """
        final_path = self.path_for(import_id)
        payload = serialize_points(import_id, list(points))
        source_names = [str(name) for name in source_files]

        tmp_path: str | None = None
        committed = False
        try:
            self.telemetry_dir.mkdir(parents=True, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(prefix=f"{TEMP_PREFIX}{import_id}-", suffix=_TEMP_SUFFIX, dir=self.telemetry_dir)
            with os.fdopen(fd, "wb") as fh:
                self._writer(fh, payload)
            os.replace(tmp_path, final_path)
            committed = True
            _fsync_directory(self.telemetry_dir)
        except Exception:
            if committed:
                # The rename succeeded; only the best-effort directory fsync failed.
                return self.relative_path(import_id)
            raise self._save_failed(import_id, source_names) from None
        finally:
            if not committed and tmp_path is not None:
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
        return self.relative_path(import_id)

    # -- load -----------------------------------------------------------------

    def load(self, import_id: str, source_files: Iterable[str] = ()) -> list[TelemetryPoint]:
        """Load and validate the persisted points of ``import_id`` (Requirement 1.8, 1.10).

        Returns:
            The points in the order they were saved.

        Raises:
            ValueError: If ``import_id`` is not a safe id.
            User_Error: ``PERSISTED_DATA_UNREADABLE`` naming ``source_files`` if
                the file is missing, cannot be read, or contains any invalid
                point. No points are returned in that case.
        """
        path = self.path_for(import_id)
        source_names = [str(name) for name in source_files]
        try:
            data = path.read_bytes()
            return deserialize_points(data, expected_import_id=import_id)
        except (OSError, ValueError):
            raise self._unreadable(import_id, source_names) from None

    # -- delete / housekeeping ---------------------------------------------------

    def delete(self, import_id: str) -> bool:
        """Remove the import's telemetry file. Returns False if it did not exist.

        Raises:
            ValueError: If ``import_id`` is not a safe id.
            OSError: If the file exists but cannot be removed.
        """
        try:
            os.remove(self.path_for(import_id))
        except FileNotFoundError:
            return False
        return True

    def list_import_ids(self) -> list[str]:
        """Sorted ids of all finished (non-temporary) telemetry files."""
        if not self.telemetry_dir.is_dir():
            return []
        ids = []
        for entry in self.telemetry_dir.iterdir():
            name = entry.name
            if entry.is_file() and name.endswith(_SUFFIX) and not name.startswith(TEMP_PREFIX):
                stem = name[: -len(_SUFFIX)]
                if _IMPORT_ID_RE.fullmatch(stem):
                    ids.append(stem)
        return sorted(ids)

    def remove_stale_temp_files(self) -> int:
        """Delete temp files left by an interrupted save. Returns the count removed."""
        if not self.telemetry_dir.is_dir():
            return 0
        removed = 0
        for entry in self.telemetry_dir.iterdir():
            if entry.name.startswith(TEMP_PREFIX) and entry.name.endswith(_TEMP_SUFFIX):
                try:
                    entry.unlink()
                    removed += 1
                except OSError:
                    pass
        return removed

    # -- errors -----------------------------------------------------------------

    def _save_failed(self, import_id: str, source_files: list[str]) -> User_Error:
        details = {"import_id": import_id, "data_dir": str(self._data_dir)}
        if source_files:
            details["source_files"] = ", ".join(source_files)
        return User_Error(
            SAVE_FAILED,
            description=f"The imported data could not be saved to the Data_Directory {self._data_dir}.",
            action=(
                "Make sure the Data_Directory is writable and has free disk space, "
                "then import the files again."
            ),
            file_name=", ".join(source_files) or None,
            details=details,
        )

    @staticmethod
    def _unreadable(import_id: str, source_files: list[str]) -> User_Error:
        names = ", ".join(source_files)
        subject = names if names else f"import {import_id}"
        return User_Error(
            PERSISTED_DATA_UNREADABLE,
            description=f"The saved telemetry of an earlier import ({subject}) could not be read or is damaged.",
            action=f"Re-import {subject}.",
            file_name=names or None,
            details={"import_id": import_id, "source_files": names},
        )


def _fsync_directory(directory: Path) -> None:
    """Best-effort fsync of a directory so the rename is durable (POSIX only)."""
    if os.name == "nt":
        return  # Windows cannot open directories for fsync.
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
