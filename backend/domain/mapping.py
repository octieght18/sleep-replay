"""Mapping defaults and shared, strict validation for configuration and settings."""
from __future__ import annotations

import math
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

from backend.domain.errors import User_Error
from backend.domain.sound import Mapping_Target
from backend.domain.telemetry import Metric

METRIC_KEYS = ("heart_rate", "hrv", "movement", "temperature", "humidity", "pressure")
FEATURE_METRICS = MappingProxyType({k: Metric("hrv_rmssd" if k == "hrv" else k) for k in METRIC_KEYS if k != "movement"})
HYSTERESIS_MAX = MappingProxyType(dict(heart_rate=10, hrv=20, temperature=1, humidity=5, pressure=1))
TARGET_DURATIONS = (30, 120, 180, 300, 600)
DEFAULT_RANDOM_SEED = 20240301
MAX_RANDOM_SEED = 2**32 - 1


def invalid_config(path: str, allowed: str) -> User_Error:
    return User_Error("INVALID_MAPPING_CONFIG", f"Invalid mapping configuration at {path}.", f"Use {allowed}.")


def number(value, low, high, path, *, integer=False):
    if type(value) not in ((int,) if integer else (int, float)) or not low <= value <= high or not math.isfinite(value):
        raise invalid_config(path, f"{'an integer' if integer else 'a finite number'} in [{low}, {high}]")
    return value


def validate_seed(value) -> int:
    if type(value) is not int or not 0 <= value <= MAX_RANDOM_SEED:
        raise User_Error("INVALID_RANDOM_SEED", "The random seed is invalid.", f"Use an integer from 0 to {MAX_RANDOM_SEED}.")
    return value


@dataclass(frozen=True)
class Metric_Mapping:
    target: Mapping_Target
    sensitivity: float
    smoothing_minutes: int | None = None
    hysteresis: float | None = None


@dataclass(frozen=True)
class Mapping_Config:
    metrics: Mapping[str, Metric_Mapping]
    target_duration: int = 180
    random_seed: int = DEFAULT_RANDOM_SEED

    def __post_init__(self):
        object.__setattr__(self, "metrics", MappingProxyType(dict(self.metrics)))
        validate_mapping(self)


def validate_mapping(config: Mapping_Config):
    if set(config.metrics) != set(METRIC_KEYS):
        raise invalid_config("metrics", "exactly these metric keys: " + ", ".join(METRIC_KEYS))
    for key, m in config.metrics.items():
        if not isinstance(m, Metric_Mapping) or not isinstance(m.target, Mapping_Target):
            raise invalid_config(key + ".target", "a target from " + ", ".join(t.value for t in Mapping_Target))
        number(m.sensitivity, 0, 1, key + ".sensitivity")
        if key == "movement":
            if m.smoothing_minutes is not None or m.hysteresis is not None:
                raise invalid_config(key, "only target and sensitivity for movement")
        else:
            number(m.smoothing_minutes, 1, 60, key + ".smoothing_minutes", integer=True)
            number(m.hysteresis, 0, HYSTERESIS_MAX[key], key + ".hysteresis")
    if type(config.target_duration) is not int or config.target_duration not in TARGET_DURATIONS:
        raise invalid_config("target_duration", "an integer from " + str(TARGET_DURATIONS))
    number(config.random_seed, 0, MAX_RANDOM_SEED, "random_seed", integer=True)


DEFAULT_MAPPING = Mapping_Config({
    "heart_rate": Metric_Mapping(Mapping_Target.pulse_rate, 0.5, 5, 1.0),
    "hrv": Metric_Mapping(Mapping_Target.modulation, 0.3, 15, 2.0),
    "movement": Metric_Mapping(Mapping_Target.transient_density, 0.8),
    "temperature": Metric_Mapping(Mapping_Target.brightness, 0.3, 15, 0.1),
    "humidity": Metric_Mapping(Mapping_Target.texture_density, 0.3, 15, 0.5),
    "pressure": Metric_Mapping(Mapping_Target.modulation, 0.2, 15, 0.1),
})


def mapping_dict(config: Mapping_Config) -> dict:
    result = {}
    for key in METRIC_KEYS:
        m = config.metrics[key]
        result[key] = {"target": m.target.value, "sensitivity": float(m.sensitivity) if m.sensitivity else 0.0}
        if key != "movement":
            result[key].update(smoothing_minutes=m.smoothing_minutes, hysteresis=float(m.hysteresis) if m.hysteresis else 0.0)
    return dict(result, target_duration=config.target_duration, random_seed=config.random_seed)


def mapping_from_dict(data: object) -> Mapping_Config:
    if not isinstance(data, dict):
        raise invalid_config("document", "a mapping of metric keys, target_duration, and random_seed")
    allowed = (*METRIC_KEYS, "target_duration", "random_seed")
    for key in data:
        if key not in allowed:
            raise invalid_config(str(key), "allowed keys: " + ", ".join(allowed))
    metrics = {}
    for key in METRIC_KEYS:
        default = DEFAULT_MAPPING.metrics[key]
        entry = data.get(key, {})
        fields = ("target", "sensitivity") if key == "movement" else ("target", "sensitivity", "smoothing_minutes", "hysteresis")
        if not isinstance(entry, dict):
            raise invalid_config(key, "a mapping with keys " + ", ".join(fields))
        for name in entry:
            if name not in fields:
                raise invalid_config(key + "." + str(name), "allowed keys: " + ", ".join(fields))
        try:
            target = Mapping_Target(entry.get("target", default.target))
        except (ValueError, TypeError):
            raise invalid_config(key + ".target", "allowed targets: " + ", ".join(t.value for t in Mapping_Target)) from None
        metrics[key] = Metric_Mapping(target, entry.get("sensitivity", default.sensitivity),
                                    entry.get("smoothing_minutes", default.smoothing_minutes),
                                    entry.get("hysteresis", default.hysteresis))
    return Mapping_Config(metrics, data.get("target_duration", 180), data.get("random_seed", DEFAULT_RANDOM_SEED))
