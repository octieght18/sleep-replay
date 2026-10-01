"""Smoke tests for the Backend package skeleton and the shared test fixtures."""

from __future__ import annotations

import importlib
import os
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# Requirement 17.1: six Backend packages (plus the persistence foundation), with
# one ingestion sub-package per Importer.
BACKEND_PACKAGES = [
    "backend.domain",
    "backend.ingestion",
    "backend.ingestion.fitbit",
    "backend.ingestion.sensorpush",
    "backend.processing",
    "backend.persistence",
    "backend.sonification",
    "backend.audio",
    "backend.api",
]


@pytest.mark.parametrize("package", BACKEND_PACKAGES)
def test_backend_package_is_importable(package: str) -> None:
    module = importlib.import_module(package)
    assert module.__file__ is not None
    assert Path(module.__file__).name == "__init__.py"


@pytest.mark.parametrize("directory", ["tools", "sample_data", "tests"])
def test_top_level_directory_exists(directory: str) -> None:
    assert (REPO_ROOT / directory).is_dir()


def test_tmp_data_dir_env_points_at_fresh_temp_directory(tmp_data_dir: Path) -> None:
    assert os.environ["SLEEP_REPLAY_DATA_DIR"] == str(tmp_data_dir)
    assert tmp_data_dir.is_dir()
    assert list(tmp_data_dir.iterdir()) == []
    # The temporary Data_Directory must lie outside the repository (Requirement 18.7).
    assert REPO_ROOT not in tmp_data_dir.resolve().parents


def test_tmp_data_dir_is_applied_without_requesting_the_fixture() -> None:
    # The fixture is autouse, so every test gets a temporary Data_Directory.
    value = os.environ.get("SLEEP_REPLAY_DATA_DIR")
    assert value
    assert REPO_ROOT not in Path(value).resolve().parents


def test_numpy_and_tzdata_available() -> None:
    import numpy  # noqa: F401
    from zoneinfo import ZoneInfo

    # tzdata makes IANA zones resolvable on Windows.
    assert ZoneInfo("America/New_York").key == "America/New_York"
