"""Slow environmental variation controls; never infer outdoor weather or risk."""

from statistics import median

from backend.domain.mapping import FEATURE_METRICS
from backend.domain.sound import Breakpoint, Mapping_Target, Parameter_Trajectory
from backend.sonification.contributions import window_spans

# Canonical input units: degrees C, relative-humidity points, hPa.
VARIATION_SCALES = {"temperature": 1.0, "humidity": 5.0, "pressure": 1.0}
VARIATION_RATE_LIMIT = 0.08  # Full excursion takes at least 12.5 replay seconds.


def nature_controls(features, config, duration):
    controls = {}
    spans = window_spans(features)
    for key, scale in VARIATION_SCALES.items():
        mapping = config.metrics[key]
        enabled = (
            key not in features.unavailable_metrics
            and mapping.target is not Mapping_Target.none
            and mapping.sensitivity > 0
        )
        values = [
            f.smoothed
            if enabled
            and (f := w.per_metric.get(FEATURE_METRICS[key]))
            and f.has_values
            and f.smoothed is not None
            else None
            for w in features.windows
        ]
        baseline = (
            median(v for v in values if v is not None)
            if any(v is not None for v in values)
            else 0
        )
        targets = [
            min(1.0, abs(v - baseline) / scale) * mapping.sensitivity
            if v is not None
            else 0.0
            for v in values
        ]
        points = [Breakpoint(0, targets[0] if targets else 0)]
        for (start, end), target in zip(spans, targets):
            time = min(duration, (start + end) / 2)
            previous = points[-1]
            if time <= previous.t:
                continue
            allowance = VARIATION_RATE_LIMIT * (time - previous.t)
            value = previous.value + max(
                -allowance, min(allowance, target - previous.value)
            )
            points.append(Breakpoint(time, value))
        if points[-1].t < duration:
            points.append(Breakpoint(duration, points[-1].value))
        controls[key] = Parameter_Trajectory(tuple(points))
    return controls
