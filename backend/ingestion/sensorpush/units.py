"""SensorPush_CSV unit-token recognition and unit inference (Requirements 6.3, 6.4, 6.14).

Header unit tokens (Input Format Assumptions), matched case-insensitively and
mapped to the ``backend.domain.units`` constants. Raw tokens are never returned:

==============  ===========================================  =================
Metric          Header tokens                                Unit constant
==============  ===========================================  =================
temperature     ``°F``, ``F``, ``Fahrenheit``                ``FAHRENHEIT``
temperature     ``°C``, ``C``, ``Celsius``                   ``CELSIUS``
humidity        ``%``                                        ``PERCENT``
pressure        ``inHg`` / ``mbar`` / ``hPa`` / ``kPa``      ``INHG`` / ``MBAR`` / ``HPA`` / ``KPA``
==============  ===========================================  =================

Alphabetic tokens are recognized only when no letter directly precedes or
follows them, so the single letters ``F`` and ``C`` count only when standalone
(``Temp (F)``, ``Temp_C``, ``°F``) and never inside words such as ``Celsius``
or ``Temperature`` (Requirement 6.3). The degree lookalikes ``º`` (U+00BA),
``℉`` (U+2109), and ``℃`` (U+2103) are treated as ``°F`` / ``°C``. If a header
contains tokens for more than one unit, the leftmost token wins.

When a header states no recognized unit, the unit is inferred from the median
of the column's parseable values and a warning names the metric and the
inferred unit (Requirement 6.4):

* temperature: median > 45 → ``°F``, otherwise ``°C``;
* pressure: 25–32 → ``inHg``, 90–110 → ``kPa``, 900–1100 → ``hPa`` (inclusive);
* humidity: always ``percent``.

A unitless pressure column whose median falls outside every range gets no unit
and a warning that the pressure unit could not be determined; the importer then
creates no pressure TelemetryPoints and keeps the other columns (6.14).

Stdlib and ``backend.domain`` only (Dependency_Rules, Requirement 17.2).
"""

from __future__ import annotations

import math
import re
import statistics
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final

from backend.domain import units as du
from backend.domain.errors import Warning_Item
from backend.domain.telemetry import Metric

__all__ = [
    "MEASUREMENT_METRICS",
    "HEADER_UNIT_TOKENS",
    "TEMPERATURE_FAHRENHEIT_THRESHOLD",
    "PRESSURE_RANGES",
    "UnitResolution",
    "unit_from_header",
    "infer_unit",
    "resolve_unit",
]

#: The metrics a SensorPush_CSV measurement column can carry.
MEASUREMENT_METRICS: Final[tuple[Metric, ...]] = (Metric.temperature, Metric.humidity, Metric.pressure)

#: Recognized header tokens per metric, for documentation and error messages.
HEADER_UNIT_TOKENS: Final[dict[Metric, tuple[str, ...]]] = {
    Metric.temperature: ("°F", "F", "Fahrenheit", "°C", "C", "Celsius"),
    Metric.humidity: ("%",),
    Metric.pressure: ("inHg", "mbar", "hPa", "kPa"),
}

#: Temperature medians strictly above this are Fahrenheit.
TEMPERATURE_FAHRENHEIT_THRESHOLD: Final[float] = 45.0

#: Inclusive median ranges for unitless pressure columns, checked in order.
PRESSURE_RANGES: Final[tuple[tuple[float, float, str], ...]] = (
    (25.0, 32.0, du.INHG),
    (90.0, 110.0, du.KPA),
    (900.0, 1100.0, du.HPA),
)

# "Not a letter" boundaries: [^\W\d_] is exactly the Unicode letters.
_NO_LETTER_BEFORE: Final = r"(?<![^\W\d_])"
_NO_LETTER_AFTER: Final = r"(?![^\W\d_])"


def _token_regex(alternatives: dict[str, str]) -> re.Pattern[str]:
    """Build one regex whose named groups map to unit constants."""
    body = "|".join(f"(?P<{group}>{pattern})" for group, pattern in alternatives.items())
    return re.compile(f"{_NO_LETTER_BEFORE}(?:{body}){_NO_LETTER_AFTER}", re.IGNORECASE)


# Group name -> (regex alternative, unit constant). Longer words come first so
# "Fahrenheit" is not read as "F" followed by letters (which would not match anyway).
_TEMPERATURE_TOKENS: Final = {"fahrenheit": ("fahrenheit|f", du.FAHRENHEIT), "celsius": ("celsius|c", du.CELSIUS)}
_PRESSURE_TOKENS: Final = {
    "inhg": ("inhg", du.INHG),
    "mbar": ("mbar", du.MBAR),
    "hpa": ("hpa", du.HPA),
    "kpa": ("kpa", du.KPA),
}

_TOKEN_PATTERNS: Final[dict[Metric, tuple[re.Pattern[str], dict[str, str]]]] = {
    metric: (
        _token_regex({group: pattern for group, (pattern, _) in table.items()}),
        {group: unit for group, (_, unit) in table.items()},
    )
    for metric, table in ((Metric.temperature, _TEMPERATURE_TOKENS), (Metric.pressure, _PRESSURE_TOKENS))
}

# Degree-sign lookalikes. "º" is a letter (category Lo), so it must be replaced
# before the letter-boundary check or "ºF" would not match.
_DEGREE_NORMALIZATION: Final = str.maketrans({"º": "°", "℉": "°F", "℃": "°C"})


def _require_measurement_metric(metric: object) -> Metric:
    if metric not in MEASUREMENT_METRICS:
        raise ValueError(f"not a SensorPush measurement metric: {metric!r}")
    return Metric(metric)


def unit_from_header(metric: Metric, header_text: str) -> str | None:
    """Return the unit constant stated in ``header_text``, or ``None``.

    ``header_text`` is the raw column header (BOM already stripped). The
    result is always one of ``ALLOWED_UNITS[metric]``.

    Raises:
        ValueError: ``metric`` is not temperature, humidity, or pressure.
    """
    metric = _require_measurement_metric(metric)
    if not isinstance(header_text, str):
        return None
    if metric is Metric.humidity:
        return du.PERCENT if "%" in header_text else None
    pattern, units = _TOKEN_PATTERNS[metric]
    match = pattern.search(header_text.translate(_DEGREE_NORMALIZATION))
    if match is None:
        return None
    return units[match.lastgroup]  # type: ignore[index]


def _finite_median(values: Iterable[float]) -> float | None:
    finite = [float(v) for v in values if isinstance(v, (int, float)) and math.isfinite(v)]
    return statistics.median(finite) if finite else None


def infer_unit(metric: Metric, values: Iterable[float]) -> str | None:
    """Infer a unit from the median of a column's parseable values.

    Humidity is always ``PERCENT``. Temperature is ``FAHRENHEIT`` when the
    median is above 45, else ``CELSIUS``. Pressure follows ``PRESSURE_RANGES``.
    Non-finite values are ignored. Returns ``None`` when there are no finite
    values (temperature, pressure) or the pressure median fits no range.

    Raises:
        ValueError: ``metric`` is not temperature, humidity, or pressure.
    """
    metric = _require_measurement_metric(metric)
    if metric is Metric.humidity:
        return du.PERCENT
    median = _finite_median(values)
    if median is None:
        return None
    if metric is Metric.temperature:
        return du.FAHRENHEIT if median > TEMPERATURE_FAHRENHEIT_THRESHOLD else du.CELSIUS
    for low, high, unit in PRESSURE_RANGES:
        if low <= median <= high:
            return unit
    return None


@dataclass(frozen=True)
class UnitResolution:
    """The unit decision for one SensorPush measurement column.

    Attributes:
        metric: The column's metric.
        unit: A ``backend.domain.units`` constant, or ``None`` when the column
            must produce no TelemetryPoints (undeterminable pressure unit, or
            no parseable values to infer from).
        inferred: ``True`` when the unit came from median inference.
        median: The median used for inference, when one was computed.
        warning: The Import_Report warning to add, if any.
    """

    metric: Metric
    unit: str | None
    inferred: bool = False
    median: float | None = None
    warning: Warning_Item | None = None


def _format_number(value: float) -> str:
    return f"{value:.2f}".rstrip("0").rstrip(".")


def resolve_unit(
    metric: Metric,
    header_text: str,
    values: Iterable[float],
    file_name: str | None = None,
) -> UnitResolution:
    """Decide the unit of one measurement column (Requirements 6.3, 6.4, 6.14).

    A recognized header token wins with no warning. Otherwise the unit is
    inferred from ``values`` (the column's parseable values) with a warning
    naming the metric and inferred unit. An undeterminable pressure unit gives
    ``unit=None`` and a warning that the pressure unit could not be
    determined. With no parseable values, temperature and pressure get
    ``unit=None`` and no warning (the column yields no points anyway).

    The warning's ``subject`` is the metric name; ``file_name``, when given, is
    named in the description.

    Raises:
        ValueError: ``metric`` is not temperature, humidity, or pressure.
    """
    metric = _require_measurement_metric(metric)
    header_unit = unit_from_header(metric, header_text)
    if header_unit is not None:
        return UnitResolution(metric, header_unit)

    in_file = f" in {file_name}" if file_name else ""
    header_label = f"'{header_text}'" if isinstance(header_text, str) else "(unreadable)"
    values = list(values)
    median = None if metric is Metric.humidity else _finite_median(values)

    if metric is not Metric.humidity and median is None:
        return UnitResolution(metric, None, inferred=False)

    unit = infer_unit(metric, values)
    if unit is None:  # only pressure can get here
        tokens = ", ".join(HEADER_UNIT_TOKENS[Metric.pressure])
        warning = Warning_Item(
            description=(
                f"The pressure unit could not be determined{in_file}: the column header {header_label} "
                f"states no unit and the median value {_format_number(median)} fits none of the "
                "inHg (25–32), kPa (90–110), or hPa (900–1100) ranges. No pressure data was imported "
                "from this file."
            ),
            subject=metric.value,
            recommended_action=(
                f"Add the pressure unit ({tokens}) to the pressure column header and import the file again."
            ),
        )
        return UnitResolution(metric, None, inferred=False, median=median, warning=warning)

    basis = "humidity is always percent" if median is None else f"the median value is {_format_number(median)}"
    warning = Warning_Item(
        description=(
            f"The {metric.value} column header {header_label}{in_file} states no unit; "
            f"the {metric.value} unit was inferred as {unit} ({basis})."
        ),
        subject=metric.value,
        recommended_action=(
            f"Check that {unit} is correct. To set the unit explicitly, add one of "
            f"{', '.join(HEADER_UNIT_TOKENS[metric])} to the column header and import the file again."
        ),
    )
    return UnitResolution(metric, unit, inferred=True, median=median, warning=warning)
