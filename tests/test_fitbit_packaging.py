"""Sanity tests for Fitbit packaging and file matching (Requirement 5).

Fuller coverage is added by the packaging unit-test task.
"""

from __future__ import annotations

import zipfile
from datetime import date

import pytest

from backend.domain.errors import (
    ARCHIVE_UNREADABLE,
    MULTIPLE_ARCHIVES,
    NO_FITBIT_FILES,
    Import_Report,
    User_Error,
)
from backend.domain.timezones import FITBIT_HRV_SUMMARY, FITBIT_SLEEP
from backend.ingestion.fitbit import packaging
from backend.ingestion.fitbit.packaging import load_fitbit_files, match_fitbit_name


def test_match_is_case_insensitive_on_final_component() -> None:
    m = match_fitbit_name("Takeout/Fitbit/Global Export Data/SLEEP-2024-01-02.JSON")
    assert m is not None and m.file_type == FITBIT_SLEEP and m.file_date == date(2024, 1, 2)
    summary = match_fitbit_name("daily heart rate variability summary - 2024-03.csv")
    assert summary is not None and summary.file_type == FITBIT_HRV_SUMMARY and summary.month_only
    assert match_fitbit_name("sleep-24-01-02.json") is None
    assert match_fitbit_name("sleep-2024-01-02.json/other.txt") is None


def test_zip_members_read_in_memory_and_unsupported_listed(tmp_path) -> None:
    archive = tmp_path / "export.ZIP"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("../../evil/sleep-2024-01-02.json", b"[]")
        zf.writestr("Takeout/readme.txt", b"hi")
        zf.writestr("Takeout/dir/", b"")
    report = Import_Report()
    files = load_fitbit_files(archive, report)
    assert [(f.name, f.data) for f in files] == [("../../evil/sleep-2024-01-02.json", b"[]")]
    assert report.unsupported_files == ["Takeout/readme.txt"] and report.unsupported_count == 1
    assert sorted(p.name for p in tmp_path.iterdir()) == ["export.ZIP"]


def test_oversized_member_is_skipped(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(packaging, "MAX_ARCHIVE_MEMBER_SIZE", 10)
    archive = tmp_path / "export.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("steps-2024-01-02.json", b"[" + b" " * 50 + b"]")
        zf.writestr("sleep-2024-01-02.json", b"[]")
    report = Import_Report()
    files = load_fitbit_files(archive, report)
    assert [f.name for f in files] == ["sleep-2024-01-02.json"]
    assert report.skipped_files[0][0] == "steps-2024-01-02.json"
    assert "10 bytes" in report.skipped_files[0][1]


@pytest.mark.parametrize(
    ("names", "code"),
    [
        (["a.zip", "b.zip"], MULTIPLE_ARCHIVES),
        (["a.zip", "sleep-2024-01-02.json"], MULTIPLE_ARCHIVES),
        (["notes.txt"], NO_FITBIT_FILES),
    ],
)
def test_packaging_errors(tmp_path, names, code) -> None:
    paths = [tmp_path / n for n in names]
    for p in paths:
        p.write_bytes(b"x")
    report = Import_Report()
    with pytest.raises(User_Error) as err:
        load_fitbit_files(paths, report)
    assert err.value.code == code
    assert report.unsupported_files == []


def test_corrupt_zip_is_unreadable(tmp_path) -> None:
    archive = tmp_path / "broken.zip"
    archive.write_bytes(b"not a zip")
    with pytest.raises(User_Error) as err:
        load_fitbit_files(archive, Import_Report())
    assert err.value.code == ARCHIVE_UNREADABLE and err.value.file_name == "broken.zip"
