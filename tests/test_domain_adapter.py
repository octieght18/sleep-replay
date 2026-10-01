"""Unit tests for backend.domain.adapter (Requirements 7.1, 7.2, 7.8, 17.4)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from backend.domain import adapter as adapter_module
from backend.domain.adapter import (
    Data_Source_Adapter,
    LoadResult,
    SupportsCandidateSessions,
    TimezoneContext,
    candidate_sessions_of,
    compose_source,
    implements_candidate_sessions,
    is_zip_source,
    source_paths,
    split_source,
)
from backend.domain.errors import INVALID_TIMEZONE, Import_Report, User_Error
from backend.domain.session import SleepSession
from backend.domain.telemetry import CANONICAL_UNIT, Metric, TelemetryPoint
from backend.domain.timezones import FITBIT_HEART_RATE, FITBIT_SLEEP, SENSORPUSH

UTC = timezone.utc
START = datetime(2024, 1, 1, 23, 0, tzinfo=UTC)


class LoadOnlyAdapter:
    source_identifier = "loadonly"

    def load(self, source, tz_context):
        point = TelemetryPoint(START, self.source_identifier, Metric.temperature, 20.0, "°C")
        return LoadResult(telemetry=[point])


class SessionAdapter(LoadOnlyAdapter):
    source_identifier = "withsessions"

    def candidate_sessions(self, source, tz_context):
        return (SleepSession(START, START + timedelta(hours=8), self.source_identifier),)


# --- Source ---------------------------------------------------------------


def test_source_paths_accepts_single_path_and_sequences():
    assert source_paths("a/export.zip") == (Path("a/export.zip"),)
    assert source_paths(Path("x.csv")) == (Path("x.csv"),)
    assert source_paths(["b.json", Path("c.csv")]) == (Path("b.json"), Path("c.csv"))
    assert source_paths([]) == ()


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("takeout.zip", True),
        (["TAKEOUT.ZIP"], True),
        (["a.zip", "b.zip"], False),
        (["a.zip", "sleep-2024-01-01.json"], False),
        (["sleep-2024-01-01.json"], False),
        ([], False),
    ],
)
def test_is_zip_source(source, expected):
    assert is_zip_source(source) is expected


# --- TelemetryPoint.source --------------------------------------------------


def test_compose_and_split_source():
    assert compose_source("sensorpush", "A1") == "sensorpush:A1"
    assert compose_source("sensorpush", " A1 ") == "sensorpush:A1"
    assert compose_source("sensorpush", "") == "sensorpush"
    assert compose_source("sensorpush", "   ") == "sensorpush"
    assert compose_source("fitbit") == "fitbit"
    assert split_source("sensorpush:A1") == ("sensorpush", "A1")
    assert split_source("sensorpush") == ("sensorpush", None)
    assert split_source(compose_source("sensorpush", "id:with:colons")) == ("sensorpush", "id:with:colons")


@pytest.mark.parametrize("bad", ["", "a:b"])
def test_compose_source_rejects_invalid_identifier(bad):
    with pytest.raises(ValueError):
        compose_source(bad, "x")


# --- TimezoneContext --------------------------------------------------------


def test_timezone_context_resolves_defaults_and_overrides():
    berlin = ZoneInfo("Europe/Berlin")
    ctx = TimezoneContext(berlin, {SENSORPUSH: "America/New_York"})
    assert ctx.source_timezone(FITBIT_SLEEP) == berlin  # Display_Timezone default
    assert ctx.source_timezone(FITBIT_HEART_RATE).key == "UTC"  # intraday default
    assert ctx.source_timezone(SENSORPUSH).key == "America/New_York"


def test_timezone_context_is_immutable_and_copies_overrides():
    overrides = {SENSORPUSH: "UTC"}
    ctx = TimezoneContext(ZoneInfo("UTC"), overrides)
    overrides[SENSORPUSH] = "Europe/Berlin"
    assert ctx.overrides[SENSORPUSH] == "UTC"
    with pytest.raises(TypeError):
        ctx.overrides[SENSORPUSH] = "Europe/Berlin"  # type: ignore[index]
    with pytest.raises(AttributeError):
        ctx.display_tz = ZoneInfo("Europe/Berlin")  # type: ignore[misc]


def test_timezone_context_invalid_override_raises_user_error():
    ctx = TimezoneContext(ZoneInfo("UTC"), {SENSORPUSH: "Mars/Olympus"})
    with pytest.raises(User_Error) as exc:
        ctx.source_timezone(SENSORPUSH)
    assert exc.value.code == INVALID_TIMEZONE


# --- LoadResult -------------------------------------------------------------


def test_load_result_defaults_are_empty_and_independent():
    a, b = LoadResult(), LoadResult()
    assert (a.telemetry, a.sessions, a.session_hrv) == ([], [], [])
    assert isinstance(a.report, Import_Report)
    a.telemetry.append(object())  # type: ignore[arg-type]
    assert b.telemetry == [] and a.report is not b.report


# --- Protocols ----------------------------------------------------------------


def test_structural_adapters_satisfy_protocol():
    assert isinstance(LoadOnlyAdapter(), Data_Source_Adapter)
    assert isinstance(SessionAdapter(), Data_Source_Adapter)
    assert not isinstance(object(), Data_Source_Adapter)
    assert isinstance(SessionAdapter(), SupportsCandidateSessions)
    assert not isinstance(LoadOnlyAdapter(), SupportsCandidateSessions)


def test_candidate_sessions_of_missing_operation_means_zero_candidates():
    ctx = TimezoneContext(ZoneInfo("UTC"))
    assert not implements_candidate_sessions(LoadOnlyAdapter())
    assert candidate_sessions_of(LoadOnlyAdapter(), ["x.csv"], ctx) == []


def test_candidate_sessions_of_calls_implemented_operation():
    ctx = TimezoneContext(ZoneInfo("UTC"))
    sessions = candidate_sessions_of(SessionAdapter(), ["x.json"], ctx)
    assert isinstance(sessions, list) and len(sessions) == 1
    assert sessions[0].source == "withsessions"


# --- Documentation (Requirement 7.8) --------------------------------------------


def test_module_docstring_documents_the_contract():
    doc = adapter_module.__doc__ or ""
    for metric in Metric:
        assert f"``{metric.value}``" in doc
        assert f"``{CANONICAL_UNIT[metric]}``" in doc
    for phrase in ("load(source, tz_context)", "candidate_sessions(source, tz_context)",
                   "TelemetryPoint fields", "Registering a new adapter", "SensorPush cloud API",
                   "unimplemented"):
        assert phrase in doc
