"""Data_Directory resolution and startup checks (Requirement 15.1, 15.2, 15.4).

The Data_Directory holds imported files, normalized telemetry, Replays, the
Metadata_Store, log files, and every temporary file derived from imported data.

Resolution order (Requirement 15.2):

1. ``SLEEP_REPLAY_DATA_DIR`` when set to a non-empty (non-whitespace) value.
   ``~`` is expanded and relative paths are made absolute against the current
   working directory.
2. Otherwise the documented per-OS default:

   - Windows: ``%LOCALAPPDATA%\\SleepReplay`` (falls back to
     ``~\\AppData\\Local\\SleepReplay`` when ``LOCALAPPDATA`` is unset)
   - macOS: ``~/Library/Application Support/SleepReplay``
   - Linux / other POSIX: ``$XDG_DATA_HOME/sleep-replay`` (falls back to
     ``~/.local/share/sleep-replay`` when ``XDG_DATA_HOME`` is unset or not absolute)

At startup, :func:`prepare_data_dir` creates the directory if missing and
verifies it is writable by creating, writing, and deleting a probe file. On
failure it raises a ``DATA_DIR_UNWRITABLE`` :class:`User_Error` naming the
path and the ``SLEEP_REPLAY_DATA_DIR`` override (Requirement 15.4).
:func:`prepare_data_dir_or_exit` wraps that for entry points (Backend, CLI):
it reports the error and exits non-zero, so callers invoke it before
accepting requests or reading any input data.

This module imports only the standard library and ``backend.domain``.
"""

from __future__ import annotations

import os
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import TextIO

from backend.domain.errors import DATA_DIR_UNWRITABLE, User_Error

__all__ = [
    "DATA_DIR_ENV",
    "APP_DIR_NAME_WINDOWS_MACOS",
    "APP_DIR_NAME_POSIX",
    "EXIT_DATA_DIR_UNWRITABLE",
    "default_data_dir",
    "resolve_data_dir",
    "check_writable",
    "prepare_data_dir",
    "prepare_data_dir_or_exit",
]

DATA_DIR_ENV = "SLEEP_REPLAY_DATA_DIR"
APP_DIR_NAME_WINDOWS_MACOS = "SleepReplay"
APP_DIR_NAME_POSIX = "sleep-replay"

# Process exit status used when the Data_Directory is unusable at startup.
EXIT_DATA_DIR_UNWRITABLE = 2

_PROBE_PREFIX = ".write-probe-"


def _home(env: Mapping[str, str]) -> Path:
    # Honour HOME / USERPROFILE from the supplied mapping so callers (and tests)
    # can compute the default without touching the real environment.
    for key in ("HOME", "USERPROFILE"):
        value = env.get(key, "").strip()
        if value:
            return Path(value)
    return Path.home()


def default_data_dir(
    platform: str | None = None,
    env: Mapping[str, str] | None = None,
) -> Path:
    """Return the documented default Data_Directory for ``platform``.

    Args:
        platform: A ``sys.platform`` value; defaults to the running platform.
        env: Environment mapping; defaults to ``os.environ``.
    """
    platform = sys.platform if platform is None else platform
    env = os.environ if env is None else env

    if platform.startswith("win"):
        local = env.get("LOCALAPPDATA", "").strip()
        base = Path(local) if local else _home(env) / "AppData" / "Local"
        return base / APP_DIR_NAME_WINDOWS_MACOS
    if platform == "darwin":
        return _home(env) / "Library" / "Application Support" / APP_DIR_NAME_WINDOWS_MACOS
    xdg = env.get("XDG_DATA_HOME", "").strip()
    # The XDG spec says relative values must be ignored.
    base = Path(xdg) if xdg and os.path.isabs(xdg) else _home(env) / ".local" / "share"
    return base / APP_DIR_NAME_POSIX


def resolve_data_dir(env: Mapping[str, str] | None = None) -> Path:
    """Resolve the Data_Directory path without touching the filesystem.

    Uses ``SLEEP_REPLAY_DATA_DIR`` when it is set to a non-empty value,
    otherwise :func:`default_data_dir`. The result is absolute.
    """
    env = os.environ if env is None else env
    override = env.get(DATA_DIR_ENV, "")
    if override.strip():
        path = Path(override.strip()).expanduser()
    else:
        path = default_data_dir(env=env)
    return Path(os.path.abspath(path))


def _unwritable_error(path: Path, reason: str) -> User_Error:
    return User_Error(
        DATA_DIR_UNWRITABLE,
        description=f"The Data_Directory {path} {reason}.",
        action=(
            f"Make {path} a writable directory, or set the {DATA_DIR_ENV} "
            "environment variable to a writable directory, then start again."
        ),
        details={"path": str(path), "env_var": DATA_DIR_ENV},
    )


def check_writable(path: Path) -> None:
    """Verify ``path`` is a writable directory by writing and removing a probe file.

    Raises:
        User_Error: ``DATA_DIR_UNWRITABLE`` if the probe cannot be written.
    """
    if not path.is_dir():
        raise _unwritable_error(path, "is not a directory")
    probe: str | None = None
    try:
        fd, probe = tempfile.mkstemp(prefix=_PROBE_PREFIX, dir=path)
        try:
            os.write(fd, b"ok")
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        raise _unwritable_error(path, "is not writable") from None
    finally:
        if probe is not None:
            try:
                os.remove(probe)
            except OSError:
                pass


def prepare_data_dir(env: Mapping[str, str] | None = None) -> Path:
    """Resolve, create (if missing), and verify the Data_Directory.

    Returns:
        The absolute Data_Directory path.

    Raises:
        User_Error: ``DATA_DIR_UNWRITABLE`` naming the path and the
            ``SLEEP_REPLAY_DATA_DIR`` override when the directory cannot be
            created or is not writable.
    """
    path = resolve_data_dir(env)
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        raise _unwritable_error(path, "cannot be created") from None
    check_writable(path)
    return path


def prepare_data_dir_or_exit(
    env: Mapping[str, str] | None = None,
    stream: TextIO | None = None,
) -> Path:
    """Startup helper for the Backend and the CLI.

    Call before accepting requests or reading any input data. On success
    returns the Data_Directory path. On failure writes the User_Error to
    ``stream`` (default ``sys.stderr``) and raises ``SystemExit`` with
    :data:`EXIT_DATA_DIR_UNWRITABLE` (non-zero).
    """
    try:
        return prepare_data_dir(env)
    except User_Error as err:
        out = sys.stderr if stream is None else stream
        print(str(err), file=out)
        raise SystemExit(EXIT_DATA_DIR_UNWRITABLE) from None
