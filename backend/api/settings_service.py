"""Validated application-wide settings, persisted without generating audio."""

import math
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from backend.domain.errors import User_Error
from backend.domain.mapping import (
    DEFAULT_MAPPING,
    METRIC_KEYS,
    mapping_dict,
    validate_seed,
)
from backend.domain.sound import Mapping_Target
from backend.processing.compression import validate_target_duration
from backend.sonification.config_parser import parse_config
import json


def invalid(name, allowed):
    return User_Error(
        "INVALID_MAPPING_CONFIG", f"Invalid setting {name}.", f"Use {allowed}."
    )


@dataclass(frozen=True)
class Settings:
    target_duration: int
    targets: dict
    sensitivities: dict
    random_seed: int
    display_units: str
    sound_style: str = "music"

    def __post_init__(self):
        validate_target_duration(self.target_duration)
        validate_seed(self.random_seed)
        if self.display_units not in ("imperial", "metric"):
            raise invalid("display_units", "imperial or metric")
        for name in ("targets", "sensitivities"):
            values = getattr(self, name)
            if not isinstance(values, Mapping) or set(values) != set(METRIC_KEYS):
                raise invalid(name, "all metric keys: " + ", ".join(METRIC_KEYS))
        for key in METRIC_KEYS:
            if not isinstance(self.targets[key], str) or self.targets[key] not in {
                t.value for t in Mapping_Target
            }:
                raise invalid(
                    "targets." + key, ", ".join(t.value for t in Mapping_Target)
                )
            value = self.sensitivities[key]
            if (
                type(value) not in (int, float)
                or not 0 <= value <= 1
                or not math.isfinite(value)
                or not math.isclose(value * 20, round(value * 20), abs_tol=1e-8)
            ):
                raise invalid(
                    "sensitivities." + key, "a number from 0.0 to 1.0 in steps of 0.05"
                )
        # Validate using the same parser as external mapping documents.
        self.to_mapping_config()
        object.__setattr__(self, "targets", MappingProxyType(dict(self.targets)))
        object.__setattr__(
            self, "sensitivities", MappingProxyType(dict(self.sensitivities))
        )

    def to_mapping_config(self):
        data = mapping_dict(DEFAULT_MAPPING)
        for key in METRIC_KEYS:
            data[key].update(
                target=self.targets[key], sensitivity=self.sensitivities[key]
            )
        data.update(target_duration=self.target_duration, random_seed=self.random_seed, sound_style=self.sound_style)
        return parse_config(json.dumps(data))

    def to_dict(self):
        return dict(
            target_duration=self.target_duration,
            targets=dict(self.targets),
            sensitivities=dict(self.sensitivities),
            random_seed=self.random_seed,
            display_units=self.display_units,
            sound_style=self.sound_style,
        )

    @classmethod
    def from_dict(cls, data):
        expected = set(DEFAULT_SETTINGS.to_dict())
        if not isinstance(data, dict) or set(data) not in (expected, expected - {"sound_style"}):
            raise invalid(
                "settings", "exactly these fields: " + ", ".join(sorted(expected))
            )
        return cls(**data)


DEFAULT_SETTINGS = Settings(
    DEFAULT_MAPPING.target_duration,
    {k: m.target.value for k, m in DEFAULT_MAPPING.metrics.items()},
    {k: m.sensitivity for k, m in DEFAULT_MAPPING.metrics.items()},
    DEFAULT_MAPPING.random_seed,
    "imperial",
)


class SettingsService:
    def __init__(self, store):
        self.store = store

    def get(self):
        data = self.store.get_setting("application_settings")
        return DEFAULT_SETTINGS if data is None else Settings.from_dict(data)

    def put(self, data):
        settings = Settings.from_dict(data)
        self.store.set_setting("application_settings", settings.to_dict())
        return settings
