"""Session-relative spans in canonical units, independent of display settings."""
from dataclasses import dataclass

from backend.domain.mapping import FEATURE_METRICS

MINIMUM_SPANS = {"heart_rate": 10.0, "hrv": 10.0, "temperature": 2.0, "humidity": 10.0, "pressure": 3.0}


@dataclass(frozen=True)
class Normalization_Span:
    low: float
    high: float

    def normalize(self, value: float) -> float:
        return min(1.0, max(0.0, (value - self.low) / (self.high - self.low)))


def normalization_span(values, minimum_width):
    values = [v for v in values if v is not None]
    low, high = (min(values), max(values)) if values else (0.0, 0.0)
    midpoint = (low + high) / 2
    width = max(high - low, minimum_width)
    return Normalization_Span(midpoint - width / 2, midpoint + width / 2)


def normalization_spans(features):
    return {key: normalization_span(
        [f.smoothed for w in features.windows if (f := w.per_metric.get(metric)) is not None and f.has_values],
        MINIMUM_SPANS[key]) for key, metric in FEATURE_METRICS.items()}
