"""Shared pytest configuration for the Sleep Replay Test_Suite.

Every test runs with ``SLEEP_REPLAY_DATA_DIR`` pointed at a fresh temporary
directory, so no test can read or modify the user's configured Data_Directory
(Requirement 18.7).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from hypothesis import settings

DATA_DIR_ENV = "SLEEP_REPLAY_DATA_DIR"

# Hypothesis profile for the whole suite:
# - database=None keeps Hypothesis from writing a .hypothesis/ example database
#   into the repository (Requirement 18.7). Failing inputs are still printed.
# - deadline=None avoids timing-based flakiness on slower machines; the overall
#   5-minute budget (Requirement 18.4) is checked at the suite level instead.
settings.register_profile("sleep-replay", database=None, deadline=None)
settings.load_profile("sleep-replay")


@pytest.fixture(autouse=True)
def tmp_data_dir(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point SLEEP_REPLAY_DATA_DIR at a fresh temporary directory for every test.

    Returns the directory path so tests can inspect what was written there.
    """
    data_dir = tmp_path_factory.mktemp("data_dir")
    monkeypatch.setenv(DATA_DIR_ENV, str(data_dir))
    return data_dir
