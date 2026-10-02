"""Replay generation, degradation, and transactional artifact/metadata storage."""
from __future__ import annotations

import os
import tempfile
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path

from backend.audio.loudness import quantize, scale_loudness
from backend.audio.renderer import render
from backend.audio.wav import wav_bytes
from backend.domain.audio_provenance import MUSIC_VERSION, replay_version
from backend.domain.errors import User_Error
from backend.domain.mapping import DEFAULT_MAPPING, FEATURE_METRICS, METRIC_KEYS, mapping_dict, validate_seed
from backend.domain.replay import Metric_Availability, Night_Event_Record, Replay_Manifest
from backend.domain.telemetry import Metric
from backend.domain.timezones import validate_iana
from backend.persistence.metadata_store import MetadataStore
from backend.processing.fingerprint import input_fingerprint as fingerprint_for
from backend.sonification.contributions import window_spans
from backend.sonification.engine import build_render_plan
from backend.sonification.manifest import serialize_manifest

REPLAY_VERSION = MUSIC_VERSION  # Legacy public music version remains stable.


@dataclass(frozen=True)
class Generation_Result:
    replay_id: str
    wav_path: Path
    manifest_path: Path
    manifest: Replay_Manifest


def state_segments(features, states):
    result = []
    for (a, b), state in zip(window_spans(features), states):
        if result and result[-1][2] == state.value:
            result[-1] = (result[-1][0], b, state.value)
        else:
            result.append((a, b, state.value))
    return tuple(result)


def metric_availability(session, timeline):
    result = {}
    for key in METRIC_KEYS:
        metric = Metric.steps if key == "movement" else FEATURE_METRICS[key]
        mask = timeline.missing.get(metric, (True,) * timeline.sample_count)
        fraction = sum(mask) / len(mask)
        unavailable = all(mask)
        if key == "hrv" and session.session_hrv is not None:
            unavailable = False
        if key == "movement" and unavailable and session.has_stage_data:
            fraction = sum(s.value == "unknown" for s in timeline.stages) / timeline.sample_count
            unavailable = False
        result[key] = Metric_Availability("unavailable" if unavailable else "available", fraction)
    return result


@contextmanager
def atomic_artifacts(artifacts):
    """Install a set of files, restoring existing files if any step/DB commit fails.

    Temporary and backup files share each destination's filesystem, making each
    rename atomic. The surrounding metadata transaction commits before cleanup.
    """
    staged, backups, installed = {}, {}, []
    try:
        for path, content in artifacts.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, name = tempfile.mkstemp(prefix=".replay-", dir=path.parent)
            temporary = Path(name)
            staged[path] = temporary
            with os.fdopen(fd, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
        for path, temporary in staged.items():
            if path.exists():
                fd, name = tempfile.mkstemp(prefix=".replay-backup-", dir=path.parent)
                os.close(fd)
                backup = Path(name)
                try:
                    os.replace(path, backup)
                except BaseException:
                    backup.unlink(missing_ok=True)
                    raise
                backups[path] = backup
            os.replace(temporary, path)
            installed.append(path)
        yield
    except BaseException:
        for path in reversed(installed):
            path.unlink(missing_ok=True)
        for path, backup in backups.items():
            if backup.exists():
                os.replace(backup, path)
        raise
    finally:
        for temporary in staged.values():
            temporary.unlink(missing_ok=True)
        for backup in backups.values():
            backup.unlink(missing_ok=True)


def generate_replay(session, timeline, features, coarse_states, night_events, *,
                    data_dir, metadata_store=None, config=DEFAULT_MAPPING, seed=None,
                    display_timezone="UTC", processing_report=None, input_fingerprint=None, output=None):
    effective_seed = validate_seed(config.random_seed if seed is None else seed)
    config = replace(config, random_seed=effective_seed)
    if not session.has_stage_data and not any(p.metric in (
        Metric.heart_rate, Metric.temperature, Metric.humidity, Metric.pressure) for p in session.telemetry):
        raise User_Error("NO_USABLE_DATA", "The session contains no usable data.",
                         "Import Fitbit sleep or heart-rate files, or a SensorPush CSV for this night.")
    zone = validate_iana(display_timezone)
    warnings = [w.description for w in processing_report.warnings] if processing_report else []
    if not session.has_stage_data:
        from backend.domain.stages import Sleep_Stage
        coarse_states = (Sleep_Stage.unknown,) * len(features)
        warnings.append("Stage data is unavailable; the Neutral preset is used for the entire replay.")
    if not any(p.metric in (Metric.temperature, Metric.humidity, Metric.pressure) for p in session.telemetry):
        warnings.append("SensorPush environmental data is absent.")
    if not session.has_stage_data and session.session_hrv is None and not any(p.metric in (Metric.heart_rate, Metric.hrv_rmssd, Metric.steps) for p in session.telemetry):
        warnings.append("Fitbit data is absent.")
    availability = metric_availability(session, timeline)
    # Session_HRV is processed context, never an extra engine input.
    features = replace(features, session_hrv=session.session_hrv,
                       unavailable_metrics=tuple(k for k in METRIC_KEYS if availability[k].status == "unavailable"))
    plan = build_render_plan(features, coarse_states, night_events, config, config.target_duration, effective_seed)
    samples = scale_loudness(render(plan))
    pcm = quantize(samples)
    del samples
    wav = wav_bytes(pcm)
    del pcm
    environmental = tuple(tuple(None if (f := w.per_metric.get(metric)) is None or not f.has_values else f.smoothed
                                for metric in (Metric.temperature, Metric.humidity, Metric.pressure)) for w in features.windows)
    manifest = Replay_Manifest(session.start_time.astimezone(zone), session.end_time.astimezone(zone), display_timezone,
        config.target_duration, features.compression_ratio, timeline.resolution_s,
        state_segments(features, coarse_states),
        tuple(Night_Event_Record(e.type.value, e.night_time.astimezone(zone), e.replay_time_s, e.magnitude, e.label) for e in night_events),
        environmental, availability, tuple(k for k in METRIC_KEYS if availability[k].status == "unavailable"),
        config, effective_seed, input_fingerprint or fingerprint_for(session), replay_version(config), tuple(dict.fromkeys(warnings)))
    manifest_bytes = serialize_manifest(manifest)
    directory = Path(data_dir).resolve()
    replay_id = uuid.uuid4().hex
    wav_path = directory / "replays" / f"{replay_id}.wav"
    manifest_path = wav_path.with_suffix(".json")
    artifacts = {wav_path: wav, manifest_path: manifest_bytes}
    if output is not None:
        output_path = Path(output).expanduser().absolute()
        if output_path.suffix.lower() != ".wav":
            raise User_Error("REPLAY_WRITE_FAILED", "The output file must have a .wav extension.", "Choose a .wav output path.", file_name=str(output_path))
        artifacts[output_path] = wav
        artifacts[output_path.with_suffix(".json")] = manifest_bytes
    owned_store = metadata_store is None
    store = metadata_store
    try:
        if owned_store:
            store = MetadataStore(directory)
        with atomic_artifacts(artifacts):
            with store.transaction():
                store.record_replay(replay_id, session_start=session.start_time, session_end=session.end_time,
                    target_duration_s=config.target_duration, wav_file=wav_path.relative_to(directory).as_posix(),
                    metadata={"manifest_file": manifest_path.relative_to(directory).as_posix(),
                              "input_fingerprint": manifest.input_fingerprint, "version": manifest.version,
                              "mapping_config": mapping_dict(config)})
    except (OSError, User_Error):
        raise User_Error("REPLAY_WRITE_FAILED", f"The replay could not be saved in Data_Directory {directory}.",
                         "Make sure the Data_Directory and output directory are writable and have free space.",
                         file_name=str(output) if output is not None else str(directory)) from None
    finally:
        if owned_store and store is not None:
            store.close()
    return Generation_Result(replay_id, wav_path if output is None else output_path,
                             manifest_path if output is None else output_path.with_suffix(".json"), manifest)
