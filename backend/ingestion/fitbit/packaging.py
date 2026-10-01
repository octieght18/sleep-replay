"""Fitbit_Export packaging: individual files or one zip, and file-name matching.

A Fitbit_Export arrives either as one or more individual files or as exactly
one ``.zip`` archive (case-insensitive suffix) (Requirement 5.1). Anything else
-- several archives, or an archive together with individual files -- is
rejected with ``MULTIPLE_ARCHIVES`` before any data is read (5.9).

Files and archive members are matched against the five supported file-name
patterns (:data:`FITBIT_FILE_PATTERNS`) on the final path component,
case-insensitively, with ``YYYY``/``MM``/``DD`` as 4/2/2 ASCII digits, at any
archive depth (5.2). Non-matching files and non-directory members are listed as
unsupported in the Import_Report with a running total (5.5). When nothing
matches, ``NO_FITBIT_FILES`` lists every pattern and the Google Takeout steps
(5.6). An archive that can't be opened gives ``ARCHIVE_UNREADABLE`` (5.10).

Archive members are streamed into memory straight from the archive; nothing is
ever written to a path derived from a member name, so absolute names and
``..`` segments are harmless (5.3, 15.1). Each matching member is limited to
:data:`MAX_ARCHIVE_MEMBER_SIZE` bytes, checked against both the declared
uncompressed size and the bytes actually produced while decompressing (5.4).
Password-protected, oversized, and undecompressable members are skipped with a
reason recorded in the Import_Report, and the import continues (5.7).

Usage::

    report = Import_Report()
    with open_fitbit_export(paths, report) as export:
        for ref in export.files:            # matched files only, in input order
            data = export.read(ref)         # None when skipped (reason recorded)
            ...

or eagerly, ``load_fitbit_files(paths, report) -> list[FitbitFile]``.

Content-level problems (unparseable JSON/CSV, non-array JSON, missing columns)
are the parsers' concern; this module only delivers bytes. Files are not added
to ``Import_Report.accepted_files`` here -- the importer does that once a file
has been parsed.

This module imports only the standard library and ``backend.domain``.
"""

from __future__ import annotations

import re
import zipfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from types import TracebackType

from backend.domain.adapter import Source, source_paths
from backend.domain.errors import (
    ARCHIVE_UNREADABLE,
    MULTIPLE_ARCHIVES,
    NO_FITBIT_FILES,
    Import_Report,
    User_Error,
)
from backend.domain.timezones import (
    FITBIT_HEART_RATE,
    FITBIT_HRV_DETAILS,
    FITBIT_HRV_SUMMARY,
    FITBIT_SLEEP,
    FITBIT_STEPS,
)

__all__ = [
    "MAX_ARCHIVE_MEMBER_SIZE",
    "FitbitPattern",
    "FITBIT_FILE_PATTERNS",
    "SUPPORTED_PATTERN_NAMES",
    "TAKEOUT_STEPS",
    "FileMatch",
    "match_fitbit_name",
    "final_component",
    "is_zip_name",
    "MemberSkipped",
    "FitbitFileRef",
    "FitbitFile",
    "FitbitExport",
    "open_fitbit_export",
    "load_fitbit_files",
]


#: Max_Archive_Member_Size: 200 MB uncompressed per archive member. Read at call
#: time, so tests can monkeypatch it.
MAX_ARCHIVE_MEMBER_SIZE: int = 200 * 1024 * 1024

# Decompression chunk size. Small enough to stop promptly on overflow.
_CHUNK_SIZE = 1024 * 1024

# Zip general-purpose flag bit 0: the member is encrypted.
_ENCRYPTED_FLAG = 0x1


# ---------------------------------------------------------------------------
# File-name patterns
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FitbitPattern:
    """One supported Fitbit file-name pattern.

    Attributes:
        file_type: File-type id from ``backend.domain.timezones``.
        display: The pattern as documented, e.g. ``sleep-YYYY-MM-DD.json``.
        regex: Case-insensitive, ASCII-digit regex with ``year``, ``month`` and
            optional ``day`` groups, matched against the whole final component.
    """

    file_type: str
    display: str
    regex: re.Pattern[str] = field(repr=False, compare=False)


def _pattern(file_type: str, display: str, prefix: str, suffix: str, day_optional: bool = False) -> FitbitPattern:
    day = r"(?:-(?P<day>[0-9]{2}))?" if day_optional else r"-(?P<day>[0-9]{2})"
    regex = re.compile(
        re.escape(prefix) + r"(?P<year>[0-9]{4})-(?P<month>[0-9]{2})" + day + re.escape(suffix),
        re.IGNORECASE | re.ASCII,
    )
    return FitbitPattern(file_type, display, regex)


#: The five supported patterns, in documentation order (Input Format Assumptions).
FITBIT_FILE_PATTERNS: tuple[FitbitPattern, ...] = (
    _pattern(FITBIT_SLEEP, "sleep-YYYY-MM-DD.json", "sleep-", ".json"),
    _pattern(FITBIT_HEART_RATE, "heart_rate-YYYY-MM-DD.json", "heart_rate-", ".json"),
    _pattern(FITBIT_STEPS, "steps-YYYY-MM-DD.json", "steps-", ".json"),
    _pattern(
        FITBIT_HRV_DETAILS,
        "Heart Rate Variability Details - YYYY-MM-DD.csv",
        "Heart Rate Variability Details - ",
        ".csv",
    ),
    _pattern(
        FITBIT_HRV_SUMMARY,
        "Daily Heart Rate Variability Summary - YYYY-MM(-DD).csv",
        "Daily Heart Rate Variability Summary - ",
        ".csv",
        day_optional=True,
    ),
)

#: Display strings of every supported pattern (used in ``NO_FITBIT_FILES``).
SUPPORTED_PATTERN_NAMES: tuple[str, ...] = tuple(p.display for p in FITBIT_FILE_PATTERNS)

#: Plain-language steps for requesting a Fitbit export through Google Takeout.
TAKEOUT_STEPS: str = (
    "To request a Fitbit export through Google Takeout: "
    "1) open takeout.google.com and sign in with the Google account linked to your Fitbit; "
    "2) click 'Deselect all', then select only 'Fitbit'; "
    "3) click 'Next step', choose 'Export once' and the .zip file type, then 'Create export'; "
    "4) when the email arrives, download the archive; "
    "5) import that .zip file here, or the extracted sleep, heart_rate, steps, and "
    "Heart Rate Variability files."
)


@dataclass(frozen=True)
class FileMatch:
    """The result of matching a name against the supported patterns.

    Attributes:
        file_type: File-type id (``FITBIT_SLEEP``, ``FITBIT_HEART_RATE``, ...).
        file_date: The date written in the file name, or ``None`` when the
            digits don't form a real calendar date (e.g. ``2024-13-40``).
            For a month-only daily HRV summary this is the 1st of the month.
        month_only: True for a ``YYYY-MM`` daily HRV summary name.
    """

    file_type: str
    file_date: date | None
    month_only: bool = False


def final_component(name: str) -> str:
    """The final path component of a file or member name (``/`` or ``\\`` separated)."""
    return re.split(r"[/\\]", name)[-1]


def is_zip_name(name: str | Path) -> bool:
    """True iff ``name`` ends in ``.zip`` (case-insensitive)."""
    return str(name).lower().endswith(".zip")


def match_fitbit_name(name: str) -> FileMatch | None:
    """Match the final path component of ``name`` against the supported patterns.

    Returns ``None`` when no pattern matches.
    """
    component = final_component(name)
    for pattern in FITBIT_FILE_PATTERNS:
        m = pattern.regex.fullmatch(component)
        if m is None:
            continue
        year, month = int(m["year"]), int(m["month"])
        month_only = m["day"] is None
        day = 1 if month_only else int(m["day"])
        try:
            file_date: date | None = date(year, month, day)
        except ValueError:
            file_date = None
        return FileMatch(pattern.file_type, file_date, month_only)
    return None


# ---------------------------------------------------------------------------
# File handles
# ---------------------------------------------------------------------------


class MemberSkipped(Exception):
    """A matched file couldn't be read; ``reason`` is the plain-language skip reason."""

    def __init__(self, name: str, reason: str) -> None:
        super().__init__(name, reason)
        self.name = name
        self.reason = reason


@dataclass(frozen=True)
class FitbitFileRef:
    """A matched Fitbit file or archive member whose bytes have not been read yet.

    Attributes:
        name: The name reported in the Import_Report: the file name for an
            individual file, the full member path for an archive member.
        file_type: File-type id (``backend.domain.timezones``).
        file_date: Date from the file name, or ``None`` when not a valid date.
        month_only: True for a ``YYYY-MM`` daily HRV summary.
    """

    name: str
    file_type: str
    file_date: date | None
    month_only: bool = False
    _reader: Callable[[], bytes] = field(default=lambda: b"", repr=False, compare=False)

    def read(self) -> bytes:
        """Return the file's bytes.

        Raises:
            MemberSkipped: the file is password-protected, larger than
                :data:`MAX_ARCHIVE_MEMBER_SIZE`, can't be decompressed, or can't
                be read. Nothing is recorded in any report.
        """
        return self._reader()


@dataclass(frozen=True)
class FitbitFile:
    """A matched Fitbit file with its contents in memory."""

    name: str
    file_type: str
    file_date: date | None
    data: bytes = field(repr=False)
    month_only: bool = False


def _format_size(n: int) -> str:
    mb = 1024 * 1024
    return f"{n // mb} MB" if n >= mb and n % mb == 0 else f"{n} bytes"


def _oversize_reason(limit: int) -> str:
    return (
        f"The file is larger than the maximum archive member size of {_format_size(limit)} "
        "when uncompressed, so it was not imported."
    )


def _read_member(zf: zipfile.ZipFile, info: zipfile.ZipInfo) -> bytes:
    """Stream one member into memory, enforcing the size limit (Requirement 5.3, 5.4)."""
    name = info.filename
    limit = MAX_ARCHIVE_MEMBER_SIZE
    if info.flag_bits & _ENCRYPTED_FLAG:
        raise MemberSkipped(name, "The file is password-protected, so it could not be read.")
    if info.file_size > limit:
        raise MemberSkipped(name, _oversize_reason(limit))
    buf = bytearray()
    try:
        with zf.open(info) as fh:
            while True:
                chunk = fh.read(min(_CHUNK_SIZE, limit + 1 - len(buf)))
                if not chunk:
                    break
                buf += chunk
                if len(buf) > limit:
                    raise MemberSkipped(name, _oversize_reason(limit))
    except MemberSkipped:
        raise
    except RuntimeError as exc:
        # zipfile raises RuntimeError for encrypted members needing a password.
        if "password" in str(exc).lower() or "encrypt" in str(exc).lower():
            raise MemberSkipped(name, "The file is password-protected, so it could not be read.") from None
        raise MemberSkipped(name, "The file could not be decompressed.") from None
    except Exception:  # noqa: BLE001 - any codec/CRC/header failure is a decompression failure
        raise MemberSkipped(name, "The file could not be decompressed.") from None
    return bytes(buf)


def _read_file(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError:
        raise MemberSkipped(path.name, "The file could not be read.") from None


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


class FitbitExport:
    """An opened Fitbit_Export. Use as a context manager; it closes the archive.

    Attributes:
        files: Matched files in input (archive) order.
        archive_name: File name of the zip archive, or ``None`` for individual files.
        report: The Import_Report that unsupported files and skips go into.
    """

    def __init__(
        self,
        files: list[FitbitFileRef],
        report: Import_Report,
        archive_name: str | None = None,
        zip_file: zipfile.ZipFile | None = None,
    ) -> None:
        self.files = files
        self.report = report
        self.archive_name = archive_name
        self._zip = zip_file

    def read(self, ref: FitbitFileRef) -> bytes | None:
        """Read ``ref``; on failure record ``(name, reason)`` in the report and return ``None``."""
        try:
            return ref.read()
        except MemberSkipped as skipped:
            self.report.skip_file(skipped.name, skipped.reason)
            return None

    def read_all(self) -> list[FitbitFile]:
        """Read every matched file, skipping (and recording) unreadable ones."""
        out: list[FitbitFile] = []
        for ref in self.files:
            data = self.read(ref)
            if data is not None:
                out.append(FitbitFile(ref.name, ref.file_type, ref.file_date, data, ref.month_only))
        return out

    def __iter__(self) -> Iterator[FitbitFileRef]:
        return iter(self.files)

    def close(self) -> None:
        if self._zip is not None:
            self._zip.close()
            self._zip = None

    def __enter__(self) -> FitbitExport:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


def _multiple_archives_error() -> User_Error:
    return User_Error(
        MULTIPLE_ARCHIVES,
        "A Fitbit import accepts either one zip archive or individual files, "
        "but this selection has more than one archive or an archive together with other files. "
        "Nothing was imported.",
        "Select only the single zip archive that contains your Fitbit data, "
        "or select the extracted Fitbit files without any archive.",
    )


def _no_fitbit_files_error(archive_name: str | None) -> User_Error:
    patterns = ", ".join(SUPPORTED_PATTERN_NAMES)
    where = "The archive contains" if archive_name else "The selected files contain"
    return User_Error(
        NO_FITBIT_FILES,
        f"{where} no Fitbit file with a supported name, so nothing was imported. "
        f"Supported file names: {patterns}.",
        f"Select files named like one of the supported patterns. {TAKEOUT_STEPS}",
        file_name=archive_name,
        details={"supported_patterns": "; ".join(SUPPORTED_PATTERN_NAMES), "takeout_steps": TAKEOUT_STEPS},
    )


def _archive_unreadable_error(archive_name: str) -> User_Error:
    return User_Error(
        ARCHIVE_UNREADABLE,
        "The zip archive could not be opened. It may be corrupted, incompletely downloaded, "
        "or not a zip file. Nothing was imported from it.",
        "Download the Fitbit export again, or extract it and select the Fitbit files directly.",
        file_name=archive_name,
    )


def _zip_reader(zf: zipfile.ZipFile, info: zipfile.ZipInfo) -> Callable[[], bytes]:
    return lambda: _read_member(zf, info)


def _path_reader(path: Path) -> Callable[[], bytes]:
    return lambda: _read_file(path)


def _open_zip(path: Path, report: Import_Report) -> FitbitExport:
    archive_name = path.name
    try:
        zf = zipfile.ZipFile(path)
    except Exception:  # noqa: BLE001 - BadZipFile, OSError, truncated/garbled headers
        raise _archive_unreadable_error(archive_name) from None
    try:
        infos = zf.infolist()
        refs: list[FitbitFileRef] = []
        unsupported: list[str] = []
        for info in infos:
            if info.is_dir():
                continue
            match = match_fitbit_name(info.filename)
            if match is None:
                unsupported.append(info.filename)
                continue
            refs.append(
                FitbitFileRef(info.filename, match.file_type, match.file_date, match.month_only, _zip_reader(zf, info))
            )
        if not refs:
            raise _no_fitbit_files_error(archive_name)
    except BaseException:
        zf.close()
        raise
    for name in unsupported:
        report.add_unsupported_file(name)
    return FitbitExport(refs, report, archive_name=archive_name, zip_file=zf)


def _open_files(paths: tuple[Path, ...], report: Import_Report) -> FitbitExport:
    refs: list[FitbitFileRef] = []
    unsupported: list[str] = []
    for path in paths:
        match = match_fitbit_name(path.name)
        if match is None:
            unsupported.append(path.name)
            continue
        refs.append(FitbitFileRef(path.name, match.file_type, match.file_date, match.month_only, _path_reader(path)))
    if not refs:
        raise _no_fitbit_files_error(None)
    for name in unsupported:
        report.add_unsupported_file(name)
    return FitbitExport(refs, report)


def open_fitbit_export(source: Source, report: Import_Report) -> FitbitExport:
    """Validate the packaging of ``source`` and list its matched Fitbit files.

    Unsupported files are added to ``report`` (name, or full member path for
    archive members, plus the running count). Nothing is decompressed until a
    file is read. The report is left untouched when a User_Error is raised.

    Raises:
        User_Error: ``MULTIPLE_ARCHIVES`` for several zips or a zip with other
            files; ``ARCHIVE_UNREADABLE`` when the zip can't be opened;
            ``NO_FITBIT_FILES`` when nothing matches a supported pattern.
    """
    paths = source_paths(source)
    zips = [p for p in paths if is_zip_name(p.name)]
    if zips and len(paths) > 1:
        raise _multiple_archives_error()
    if zips:
        return _open_zip(zips[0], report)
    return _open_files(paths, report)


def load_fitbit_files(source: Source, report: Import_Report) -> list[FitbitFile]:
    """Eager form of :func:`open_fitbit_export`: read every matched file into memory.

    Skipped files are recorded in ``report``. Raises the same User_Errors.
    """
    with open_fitbit_export(source, report) as export:
        return export.read_all()
