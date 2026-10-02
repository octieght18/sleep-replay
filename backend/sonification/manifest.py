"""Canonical manifest JSON and strict validation of the public replay metadata."""
from __future__ import annotations

import json
import math
from dataclasses import fields
from datetime import datetime, timezone

from backend.domain.errors import User_Error
from backend.domain.audio_provenance import nature_provenance
from backend.domain.events import Event_Type
from backend.domain.mapping import METRIC_KEYS, TARGET_DURATIONS, mapping_dict, mapping_from_dict, validate_seed
from backend.domain.replay import Metric_Availability, Night_Event_Record, Replay_Manifest
from backend.domain.stages import Sleep_Stage
from backend.domain.timezones import validate_iana
from backend.domain.timeline import TIMELINE_RESOLUTION_CANDIDATES_S


def manifest_dict(manifest):
    zone = validate_iana(manifest.display_timezone)
    def timestamp(t):
        return t.astimezone(zone).isoformat(timespec="microseconds" if t.microsecond else "seconds")
    result = dict(
        session_start=timestamp(manifest.session_start), session_end=timestamp(manifest.session_end),
        display_timezone=manifest.display_timezone, target_duration_s=manifest.target_duration_s,
        compression_ratio=float(manifest.compression_ratio), timeline_resolution_s=manifest.timeline_resolution_s,
        coarse_states=[[float(a), float(b), state] for a, b, state in manifest.coarse_states],
        night_events=[dict(type=e.type, night_time=timestamp(e.night_time), replay_time_s=float(e.replay_time_s),
                           magnitude=float(e.magnitude), label=e.label) for e in manifest.night_events],
        environmental_windows=[[None if v is None else float(v) for v in w] for w in manifest.environmental_windows],
        availability={k: dict(status=v.status, missing_fraction=float(v.missing_fraction)) for k, v in manifest.availability.items()},
        unavailable_metrics=list(manifest.unavailable_metrics), mapping_config=mapping_dict(manifest.mapping_config),
        random_seed=manifest.random_seed, input_fingerprint=manifest.input_fingerprint, version=manifest.version,
        warnings=list(manifest.warnings))
    if manifest.mapping_config.sound_style == "nature":
        result["sound_provenance"] = nature_provenance()
    return result


def serialize_manifest(manifest) -> bytes:
    data = manifest_dict(manifest)
    _from_dict(data)
    return (json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")


def _number(value, field, low=0, high=math.inf, integer=False):
    if type(value) not in ((int,) if integer else (int, float)) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f"Invalid manifest field {field}: finite {'integer' if integer else 'number'} in [{low}, {high}] required")
    return value


def _sequence(value, field):
    if not isinstance(value, list):
        raise ValueError(f"Invalid manifest field {field}: list required")
    return value


def _from_dict(data):
    required = {f.name for f in fields(Replay_Manifest)}
    if not isinstance(data, dict):
        raise ValueError("Invalid manifest document: object required")
    config_data = data.get("mapping_config")
    if isinstance(config_data, dict) and config_data.get("sound_style") == "nature":
        required.add("sound_provenance")
        if data.get("sound_provenance") != nature_provenance():
            raise ValueError("Invalid manifest field sound_provenance: original CC0 nature generator required")
    missing, unknown = required - data.keys(), data.keys() - required
    if missing or unknown:
        raise ValueError(f"Invalid manifest fields; missing: {sorted(missing)}, unknown: {sorted(unknown)}")
    zone = validate_iana(data["display_timezone"])
    def timestamp(value, field):
        try:
            dt = datetime.fromisoformat(value)
        except (ValueError, TypeError):
            raise ValueError(f"Invalid manifest field {field}: ISO 8601 timestamp required") from None
        if dt.utcoffset() is None or dt.utcoffset() != dt.astimezone(zone).utcoffset():
            raise ValueError(f"Invalid manifest field {field}: Display_Timezone offset required")
        return dt
    start, end = timestamp(data["session_start"], "session_start"), timestamp(data["session_end"], "session_end")
    if end.astimezone(timezone.utc) <= start.astimezone(timezone.utc):
        raise ValueError("Invalid manifest field session_end: must follow session_start")
    duration = _number(data["target_duration_s"], "target_duration_s", integer=True)
    if duration not in TARGET_DURATIONS:
        raise ValueError("Invalid manifest field target_duration_s")
    ratio = _number(data["compression_ratio"], "compression_ratio", low=1)
    expected_ratio = (end.astimezone(timezone.utc) - start.astimezone(timezone.utc)).total_seconds() / duration
    if not math.isclose(ratio, expected_ratio, rel_tol=1e-12):
        raise ValueError("Invalid manifest field compression_ratio: disagrees with session duration")
    resolution = _number(data["timeline_resolution_s"], "timeline_resolution_s", integer=True)
    if resolution not in TIMELINE_RESOLUTION_CANDIDATES_S:
        raise ValueError("Invalid manifest field timeline_resolution_s")
    states = []
    previous = 0
    for i, row in enumerate(_sequence(data["coarse_states"], "coarse_states")):
        if not isinstance(row, list) or len(row) != 3:
            raise ValueError(f"Invalid manifest field coarse_states[{i}]")
        a, b, state = row
        _number(a, f"coarse_states[{i}].start", high=duration)
        _number(b, f"coarse_states[{i}].end", high=duration)
        if a != previous or b <= a or state not in {s.value for s in Sleep_Stage}:
            raise ValueError(f"Invalid manifest field coarse_states[{i}]: ordered gap-free coverage required")
        states.append((a, b, state))
        previous = b
    if previous != duration:
        raise ValueError("Invalid manifest field coarse_states: must cover target_duration_s")
    events = []
    for i, row in enumerate(_sequence(data["night_events"], "night_events")):
        path = f"night_events[{i}]"
        if not isinstance(row, dict) or set(row) != {f.name for f in fields(Night_Event_Record)}:
            raise ValueError(f"Invalid manifest field {path}: event fields required")
        time = timestamp(row["night_time"], path + ".night_time")
        if not start.astimezone(timezone.utc) <= time.astimezone(timezone.utc) <= end.astimezone(timezone.utc):
            raise ValueError(f"Invalid manifest field {path}.night_time")
        _number(row["replay_time_s"], path + ".replay_time_s", high=duration)
        _number(row["magnitude"], path + ".magnitude", high=1)
        if row["type"] not in {e.value for e in Event_Type} or not isinstance(row["label"], str):
            raise ValueError(f"Invalid manifest field {path}.type or label")
        events.append(Night_Event_Record(row["type"], time, row["replay_time_s"], row["magnitude"], row["label"]))
    windows = []
    for i, row in enumerate(_sequence(data["environmental_windows"], "environmental_windows")):
        if not isinstance(row, list) or len(row) != 3:
            raise ValueError(f"Invalid manifest field environmental_windows[{i}]")
        for v in row:
            if v is not None:
                _number(v, f"environmental_windows[{i}]", low=-math.inf)
        windows.append(tuple(row))
    if len(windows) != duration * 2:
        raise ValueError("Invalid manifest field environmental_windows: one entry per half-second feature window required")
    availability = data["availability"]
    if not isinstance(availability, dict) or set(availability) != set(METRIC_KEYS):
        raise ValueError("Invalid manifest field availability: all six metric keys required")
    available = {}
    for k, row in availability.items():
        if not isinstance(row, dict) or set(row) != {"status", "missing_fraction"} or row["status"] not in ("available", "unavailable"):
            raise ValueError(f"Invalid manifest field availability.{k}")
        _number(row["missing_fraction"], "availability." + k + ".missing_fraction", high=1)
        available[k] = Metric_Availability(**row)
    unavailable = _sequence(data["unavailable_metrics"], "unavailable_metrics")
    if len(unavailable) != len(set(unavailable)) or set(unavailable) != {k for k, v in available.items() if v.status == "unavailable"}:
        raise ValueError("Invalid manifest field unavailable_metrics")
    config = mapping_from_dict(data["mapping_config"])
    seed = validate_seed(data["random_seed"])
    if config.random_seed != seed or config.target_duration != duration:
        raise ValueError("Invalid manifest field mapping_config: effective seed and duration required")
    warnings = _sequence(data["warnings"], "warnings")
    if any(not isinstance(w, str) for w in warnings):
        raise ValueError("Invalid manifest field warnings")
    for name in ("version", "input_fingerprint"):
        if not isinstance(data[name], str) or not data[name]:
            raise ValueError(f"Invalid manifest field {name}")
    return Replay_Manifest(start, end, data["display_timezone"], duration, ratio, resolution, tuple(states), tuple(events),
                           tuple(windows), available, tuple(unavailable), config, seed, data["input_fingerprint"], data["version"], tuple(warnings))


def parse_manifest(document):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError(f"Invalid manifest field {key}: duplicate key")
            result[key] = value
        return result
    try:
        return _from_dict(json.loads(document, object_pairs_hook=pairs))
    except (TypeError, KeyError, UnicodeError, json.JSONDecodeError, User_Error) as err:
        raise ValueError(f"Invalid manifest document or field: {err}") from None
