"""Property test for the adapter validation gate (task 5.2).

Feature: sleep-replay-data-pipeline, Property 2: Invalid returned TelemetryPoints are excluded, valid ones retained

*For any* list of TelemetryPoints returned by an adapter load operation, the
Backend retains exactly the valid points (timezone-aware timestamp, MVP metric,
finite value, unit convertible to the metric's Canonical_Unit), excludes every
invalid point, and the reported excluded count per reason equals the number of
points failing for that reason.

**Validates: Requirements 7.7**

The generators build each point from a valid base and then apply zero or more
known defects, so every point carries its expected exclusion reason by
construction (the test does not re-derive it from the gate's own checks). A
point with several defects is expected under the first one in
``EXCLUSION_REASONS`` order, which is the gate's documented contract.
"""

from __future__ import annotations

import tempfile
from collections import Counter
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st

from backend.domain.adapter import LoadResult
from backend.domain.errors import Import_Report
from backend.domain.telemetry import Metric, TelemetryPoint
from backend.domain.units import ALLOWED_UNITS
from backend.ingestion.registry import (
    EXCLUDED_INCONVERTIBLE_UNIT,
    EXCLUDED_INVALID_SOURCE,
    EXCLUDED_MALFORMED_POINT,
    EXCLUDED_NAIVE_TIMESTAMP,
    EXCLUDED_NON_FINITE_VALUE,
    EXCLUDED_NON_MVP_METRIC,
    EXCLUSION_REASONS,
    AdapterRegistry,
    ImportRecord,
    ImportStores,
    apply_validation_gate,
    exclusion_reason,
)
from backend.persistence.telemetry_store import TelemetryStore

# ---------------------------------------------------------------------------
# Valid points
# ---------------------------------------------------------------------------

_MAX_OFFSET_S = 24 * 3600 - 1  # timezone() requires |offset| < 24h

_ZONES = ["UTC", "America/New_York", "Europe/London", "Asia/Kolkata", "Asia/Kathmandu", "Australia/Lord_Howe"]

valid_tzinfos = st.one_of(
    st.just(timezone.utc),
    st.integers(-23 * 60 - 59, 23 * 60 + 59).map(lambda m: timezone(timedelta(minutes=m))),
    st.integers(-_MAX_OFFSET_S, _MAX_OFFSET_S).map(lambda s: timezone(timedelta(seconds=s))),
    st.sampled_from(_ZONES).map(ZoneInfo),
)

naive_datetimes = st.datetimes(min_value=datetime(1970, 1, 1), max_value=datetime(2100, 12, 31))

valid_timestamps = st.datetimes(
    min_value=datetime(1970, 1, 1), max_value=datetime(2100, 12, 31), timezones=valid_tzinfos
)

metric_units = st.sampled_from([(m, u) for m in Metric for u in sorted(ALLOWED_UNITS[m])])

finite_values = st.one_of(
    st.integers(-(2**1000), 2**1000),
    st.floats(allow_nan=False, allow_infinity=False),
)

valid_sources = st.one_of(
    st.sampled_from(["fitbit", "sensorpush", "sensorpush:HT1-0042"]),
    st.text(min_size=1, max_size=20),
)


@st.composite
def valid_points(draw: st.DrawFn) -> TelemetryPoint:
    metric, unit = draw(metric_units)
    return TelemetryPoint(
        timestamp=draw(valid_timestamps),
        source=draw(valid_sources),
        metric=metric,
        value=draw(finite_values),
        unit=unit,
    )


# ---------------------------------------------------------------------------
# Field defects (each maps to exactly one exclusion reason)
# ---------------------------------------------------------------------------


@st.composite
def sub_second_offset_timestamps(draw: st.DrawFn) -> datetime:
    # Whole seconds in [-86399, 86398] plus 1..999999 us stays strictly inside ±24h.
    offset = timedelta(seconds=draw(st.integers(-_MAX_OFFSET_S, _MAX_OFFSET_S - 1)),
                       microseconds=draw(st.integers(1, 999_999)))
    return draw(naive_datetimes).replace(tzinfo=timezone(offset))


bad_timestamps = st.one_of(
    naive_datetimes,  # no tzinfo at all
    sub_second_offset_timestamps(),  # counted as naive_timestamp
    st.sampled_from([None, "2024-01-01T22:00:00+01:00", 1704142800]),  # not a datetime
)

non_mvp_metrics = st.one_of(
    # Plain strings are not Metric members, even the MVP wire names.
    st.sampled_from(["sleep_stage", "spo2", "HEART_RATE", "heart_rate", "temperature", "", None, 0]),
    st.text(max_size=12),
)

non_finite_values = st.sampled_from(
    [
        float("nan"),
        -float("nan"),
        float("inf"),
        -float("inf"),
        True,  # bool is an int subclass but not a measurement
        False,
        10**400,  # too large for a float
        -(10**400),
        "60",
        None,
        Decimal("60"),
        complex(1, 0),
    ]
)

_ALL_UNITS = sorted({u for units in ALLOWED_UNITS.values() for u in units})


def inconvertible_units(metric: Metric) -> st.SearchStrategy[object]:
    allowed = ALLOWED_UNITS[metric]
    return st.one_of(
        st.sampled_from([u for u in _ALL_UNITS if u not in allowed]),  # another metric's unit
        st.sampled_from(["BPM", "F", "°c", "mmHg", "", None, 1]),  # near misses / wrong types
        st.text(max_size=8).filter(lambda u: u not in allowed),
    )


invalid_sources = st.sampled_from(["", "", "", None, 42, b"fitbit"])  # mostly empty strings

# Reason for each field defect, in EXCLUSION_REASONS order.
_FIELD_DEFECTS = (
    EXCLUDED_NAIVE_TIMESTAMP,
    EXCLUDED_NON_MVP_METRIC,
    EXCLUDED_NON_FINITE_VALUE,
    EXCLUDED_INCONVERTIBLE_UNIT,
    EXCLUDED_INVALID_SOURCE,
)


@st.composite
def defective_points(draw: st.DrawFn, defects: frozenset[str]) -> TelemetryPoint:
    base = draw(valid_points())
    changes: dict[str, object] = {}
    if EXCLUDED_NAIVE_TIMESTAMP in defects:
        changes["timestamp"] = draw(bad_timestamps)
    if EXCLUDED_NON_MVP_METRIC in defects:
        changes["metric"] = draw(non_mvp_metrics)
    if EXCLUDED_NON_FINITE_VALUE in defects:
        changes["value"] = draw(non_finite_values)
    if EXCLUDED_INCONVERTIBLE_UNIT in defects:
        # Chosen against the base metric; if the metric is also broken, non_mvp_metric wins anyway.
        changes["unit"] = draw(inconvertible_units(base.metric))
    if EXCLUDED_INVALID_SOURCE in defects:
        changes["source"] = draw(invalid_sources)
    return replace(base, **changes)


# ---------------------------------------------------------------------------
# Malformed objects (not TelemetryPoints at all)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LookalikePoint:
    """Has every TelemetryPoint field but is not a TelemetryPoint."""

    timestamp: datetime
    source: str
    metric: Metric
    value: float
    unit: str


@st.composite
def malformed_objects(draw: st.DrawFn) -> object:
    p = draw(valid_points())
    fields = (p.timestamp, p.source, p.metric, p.value, p.unit)
    return draw(
        st.sampled_from(
            [
                None,
                0,
                "heart_rate,60,bpm",
                object(),
                fields,  # tuple of valid fields
                dict(zip(("timestamp", "source", "metric", "value", "unit"), fields)),
                LookalikePoint(*fields),
            ]
        )
    )


# ---------------------------------------------------------------------------
# Tagged mixes: (object, expected reason or None)
# ---------------------------------------------------------------------------

single_defect = st.sampled_from(_FIELD_DEFECTS).map(lambda d: frozenset({d}))
multi_defect = st.sets(st.sampled_from(_FIELD_DEFECTS), min_size=2).map(frozenset)


def _expected_for(defects: frozenset[str]) -> str:
    return next(r for r in EXCLUSION_REASONS if r in defects)


tagged_points = st.one_of(
    valid_points().map(lambda p: (p, None)),
    valid_points().map(lambda p: (p, None)),  # weight valid points up
    single_defect.flatmap(lambda d: defective_points(d).map(lambda p: (p, _expected_for(d)))),
    single_defect.flatmap(lambda d: defective_points(d).map(lambda p: (p, _expected_for(d)))),
    multi_defect.flatmap(lambda d: defective_points(d).map(lambda p: (p, _expected_for(d)))),
    malformed_objects().map(lambda o: (o, EXCLUDED_MALFORMED_POINT)),
)

tagged_mixes = st.lists(tagged_points, max_size=40)


def _expected(tagged: list[tuple[object, str | None]]) -> tuple[list[object], dict[str, int]]:
    retained = [obj for obj, reason in tagged if reason is None]
    counts = dict(Counter(reason for _, reason in tagged if reason is not None))
    return retained, counts


def _assert_same_objects(got: object, want: list[object]) -> None:
    got = list(got)  # type: ignore[call-overload]
    assert len(got) == len(want)
    # Identity, in order: the gate must pass the very same objects through.
    assert all(g is w for g, w in zip(got, want))


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------

_EDGE_POINT = TelemetryPoint(datetime(2024, 1, 1, tzinfo=timezone.utc), "fitbit", Metric.heart_rate, 60, "bpm")


# Feature: sleep-replay-data-pipeline, Property 2: Invalid returned TelemetryPoints are excluded, valid ones retained
@settings(max_examples=300)
@given(tagged=tagged_mixes)
@example(tagged=[])
@example(tagged=[(replace(_EDGE_POINT, timestamp=_EDGE_POINT.timestamp.replace(
    tzinfo=timezone(timedelta(microseconds=1)))), EXCLUDED_NAIVE_TIMESTAMP)])
@example(tagged=[(replace(_EDGE_POINT, value=True), EXCLUDED_NON_FINITE_VALUE), (_EDGE_POINT, None)])
def test_validation_gate_retains_exactly_valid_points(tagged: list[tuple[object, str | None]]) -> None:
    """Property 2 via apply_validation_gate. **Validates: Requirements 7.7**"""
    points = [obj for obj, _ in tagged]
    want_retained, want_counts = _expected(tagged)

    for obj, reason in tagged:
        assert exclusion_reason(obj) == reason, repr(obj)

    gate = apply_validation_gate(points)
    _assert_same_objects(gate.retained, want_retained)
    assert dict(gate.excluded) == want_counts
    assert set(gate.excluded) <= set(EXCLUSION_REASONS)
    assert all(count > 0 for count in gate.excluded.values())
    assert gate.excluded_count + len(gate.retained) == len(points)


class _FakeAdapter:
    source_identifier = "fake"

    def __init__(self, points: list[object]) -> None:
        self.points = points

    def load(self, source, tz_context) -> LoadResult:
        return LoadResult(telemetry=list(self.points), report=Import_Report(accepted_files=["export.csv"]))


# Feature: sleep-replay-data-pipeline, Property 2: Invalid returned TelemetryPoints are excluded, valid ones retained
@settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(tagged=tagged_mixes)
@example(tagged=[])
def test_run_import_reports_gate_counts(tagged: list[tuple[object, str | None]], tmp_data_dir: Path) -> None:
    """Property 2 via run_import with a fake adapter. **Validates: Requirements 7.7**"""
    points = [obj for obj, _ in tagged]
    want_retained, want_counts = _expected(tagged)

    registry = AdapterRegistry()
    registry.register(_FakeAdapter(points))
    records: list[ImportRecord] = []
    # tmp_data_dir is shared across Hypothesis examples, so each example gets
    # its own fresh Data_Directory beneath it.
    with tempfile.TemporaryDirectory(dir=tmp_data_dir) as example_dir:
        store = TelemetryStore(example_dir)
        result = registry.run_import("fake", ["dir/export.csv"], None, "UTC", ImportStores(store, records.append))

        _assert_same_objects(result.telemetry, want_retained)
        assert result.report.excluded_point_counts == want_counts
        assert sum(result.report.excluded_point_counts.values()) + result.record.point_count == len(points)
        assert result.record.point_count == len(want_retained)
        assert result.report.per_metric_counts == dict(Counter(p.metric.value for p in want_retained))
        assert records == [result.record]

        loaded = store.load(result.import_id)
        assert [(p.source, p.metric, p.unit) for p in loaded] == [
            (p.source, p.metric, p.unit) for p in want_retained
        ]
