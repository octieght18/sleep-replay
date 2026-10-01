"""Metric contributions with hysteresis and interruptible missing-data glides."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timezone

from backend.domain.mapping import FEATURE_METRICS
from backend.domain.sound import Breakpoint, Mapping_Target, Parameter_Trajectory

NEUTRAL_VALUE = 0.5
GLIDE_DURATION_S = 1.0


def mapped_value(normalized, sensitivity, key):
    excursion = sensitivity * (min(1.0, max(0.0, normalized)) - 0.5)
    return min(1.0, max(0.0, 0.5 + excursion * (0.5 if key == "pressure" else 1.0)))


@dataclass
class Hysteresis_Reference:
    threshold: float
    reference: float | None = None
    applied: float = NEUTRAL_VALUE

    def update(self, value, contribution):
        if self.reference is None or abs(value - self.reference) >= self.threshold:
            self.reference, self.applied = value, contribution
        return self.applied


def window_spans(features):
    if not features.windows:
        return ()
    start = features.windows[0].start_time.astimezone(timezone.utc)
    return tuple(((w.start_time.astimezone(timezone.utc) - start).total_seconds() / features.compression_ratio,
                  (w.end_time.astimezone(timezone.utc) - start).total_seconds() / features.compression_ratio)
                 for w in features.windows)


def contribution_trajectory(features, key, mapping, span, duration):
    if key in features.unavailable_metrics or mapping.target is Mapping_Target.none or mapping.sensitivity == 0:
        return Parameter_Trajectory((Breakpoint(0, 0.5), Breakpoint(duration, 0.5)))
    if key == "hrv" and features.session_hrv is not None and not any(
        (f := w.per_metric.get(FEATURE_METRICS[key])) and f.has_values for w in features.windows
    ):
        value = mapped_value(features.session_hrv / 100, mapping.sensitivity, key)
        return Parameter_Trajectory((Breakpoint(0, value), Breakpoint(duration, value)))

    points = []
    was_missing = None
    hysteresis = Hysteresis_Reference(mapping.hysteresis or 0)
    for w, (start, end) in zip(features.windows, window_spans(features)):
        f = None if key == "movement" else w.per_metric.get(FEATURE_METRICS[key])
        missing = key != "movement" and (f is None or not f.has_values or f.smoothed is None)
        if missing:
            value = 0.5
        else:
            if was_missing is True:
                hysteresis.reference = None
            raw = w.movement_intensity if key == "movement" else f.smoothed
            normalized = raw if key == "movement" else span.normalize(raw)
            value = hysteresis.update(raw, mapped_value(normalized, mapping.sensitivity, key))
        center = (start + end) / 2
        if was_missing is None:
            points = [Breakpoint(0, value)]
            points.append(Breakpoint(center, value))
        elif missing != was_missing:
            current = Parameter_Trajectory(tuple(points)).value_at(start)
            points = [p for p in points if p.t < start]
            points.extend((Breakpoint(start, current), Breakpoint(start + GLIDE_DURATION_S, value)))
            if not missing:
                hysteresis.reference = raw
                hysteresis.applied = value
        elif points[-1].t < center:
            points.append(Breakpoint(center, value))
        was_missing = missing
    if not points:
        points = [Breakpoint(0, 0.5)]
    if points[-1].t < duration:
        points.append(Breakpoint(duration, points[-1].value))
    return Parameter_Trajectory(tuple(points))
