"""Local, restart-safe data pipeline service. HTTP and audio belong to later specs.

Import operations commit telemetry, provenance, candidates, and selection together.
NO_SLEEP_SESSION is the explicit exception: imported telemetry is kept so a manual
range can be supplied later (requirement 8.7). Processing performs no storage writes.
"""
from __future__ import annotations

import os
import math
import uuid
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path
from typing import Mapping

from backend.domain.adapter import SessionHrvRow, Source
from backend.domain.errors import (
    INSUFFICIENT_DATA, NO_SLEEP_SESSION, PERSISTED_DATA_UNREADABLE, SOURCE_LOAD_FAILED,
    Import_Report, Processing_Report, User_Error, merge_import_reports, merge_processing_reports,
)
from backend.domain.events import Night_Event
from backend.domain.features import Coarse_State, Feature_Series
from backend.domain.sample_dataset import SAMPLE_TIMEZONE
from backend.domain.session import SleepSession
from backend.domain.telemetry import Metric
from backend.domain.timeline import Aligned_Timeline
from backend.domain.timezones import (
    FITBIT_HEART_RATE, FITBIT_STEPS, FITBIT_HRV_DETAILS, FITBIT_HRV_SUMMARY, FITBIT_SLEEP, SENSORPUSH,
)
from backend.ingestion.fitbit.hrv import session_hrv_for
from backend.ingestion.fitbit.importer import FitbitImporter, add_missing_metric_warnings, files_in_selection_window
from backend.ingestion.sensorpush.importer import SensorPushImporter
from backend.ingestion.registry import AdapterRegistry, ImportRecord, ImportStores
from backend.persistence.data_directory import prepare_data_dir
from backend.persistence.metadata_store import FileRecord, MetadataStore
from backend.persistence.telemetry_store import TelemetryStore
from backend.processing.aligner import align
from backend.processing.compression import Night_Compression, night_compression, select_target_duration
from backend.processing.events import detect
from backend.processing.features import extract
from backend.processing.fingerprint import input_fingerprint
from backend.processing.session_detector import (
    Selection, attach_telemetry, choose_session, dedup_and_merge, define_manual_session,
    no_sleep_session_error, resolve_selection,
)
from backend.processing.timezones import resolve_display_timezone


@dataclass(frozen=True)
class ProcessResult:
    session: SleepSession
    timeline: Aligned_Timeline
    features: Feature_Series
    coarse_states: tuple[Coarse_State, ...]
    events: tuple[Night_Event, ...]
    report: Processing_Report
    compression: Night_Compression
    input_fingerprint: str


class Pipeline:
    """Call import_files/use_sample_data, then process; close to release SQLite.

    A custom adapter can be registered through ``pipeline.registry.register``.
    ``select_session`` and ``define_manual_range`` persist explicit selections.
    All paths written by this service are inside its Data_Directory.
    """

    def __init__(self, data_dir: str | Path | None = None, *, registry: AdapterRegistry | None = None, display_timezone: str | None = None):
        env = dict(os.environ)
        if data_dir is not None:
            env["SLEEP_REPLAY_DATA_DIR"] = str(data_dir)
        if display_timezone is not None:
            from backend.domain.timezones import validate_iana
            validate_iana(display_timezone)
            env["SLEEP_REPLAY_DISPLAY_TIMEZONE"] = display_timezone
        self.data_dir = prepare_data_dir(env)
        self.display_timezone = resolve_display_timezone(env)
        self.display_tz = self.display_timezone.zone
        self.registry = registry if registry is not None else AdapterRegistry()
        for adapter in (FitbitImporter(), SensorPushImporter()):
            if adapter.source_identifier not in self.registry:
                self.registry.register(adapter)
        self.telemetry_store = TelemetryStore(self.data_dir)
        self.metadata_store = MetadataStore(self.data_dir)
        self.stores = ImportStores(self.telemetry_store, self._record_import)
        self.candidates: list[SleepSession] = []
        self.selected_session: SleepSession | None = None
        try:
            candidates, selection = self._discover(Import_Report())
            self._publish(candidates, selection)
        except BaseException:
            self.close()
            raise

    def close(self):
        self.metadata_store.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _record_import(self, record: ImportRecord):
        if not record.point_count and not record.candidate_sessions:
            raise User_Error(
                SOURCE_LOAD_FAILED, "No readable telemetry or sleep logs were found in this import.",
                "Choose a supported export containing measurements or sleep logs and import it again.",
                file_name=", ".join(record.source_files) or None,
            )
        names = list(dict.fromkeys((*record.source_files, *record.report.accepted_files, *record.file_dates)))
        self.metadata_store.record_import(
            record.import_id, record.source_identifier,
            [FileRecord(name, record.file_dates.get(name)) for name in names],
            telemetry_file=record.telemetry_ref, import_report=record.report,
            sessions=record.candidate_sessions,
            context={
                "point_files": list(record.point_files),
                "session_hrv": [{"date": r.date.isoformat(), "rmssd": r.rmssd, "source_file": r.source_file}
                                for r in record.session_hrv],
                "display_timezone": record.display_timezone,
                "source_timezone_overrides": dict(record.source_timezone_overrides),
            },
        )

    def _load_points(self, session: SleepSession | None = None):
        points, rows = [], []
        for record in self.metadata_store.list_imports():
            imported = self.telemetry_store.load(record.import_id, record.source_files)
            context = self.metadata_store.get_import_context(record.import_id)
            try:
                point_files = context.get("point_files", [])
                if not isinstance(point_files, list) or any(not isinstance(f, str) or not f for f in point_files):
                    raise ValueError("invalid provenance")
                if point_files and len(point_files) != len(imported):
                    raise ValueError("invalid provenance length")
                daily = [SessionHrvRow(date.fromisoformat(r["date"]), float(r["rmssd"]), r["source_file"])
                         for r in context.get("session_hrv", [])]
                if any(not math.isfinite(r.rmssd) or not 0 < r.rmssd <= 500 for r in daily):
                    raise ValueError("invalid daily HRV")
                if session is not None and record.source_identifier == "fitbit":
                    allowed = set(files_in_selection_window(
                        record.file_dates, session.start_time, session.end_time, self.display_tz))
                    if point_files:
                        imported = [p for p, name in zip(imported, point_files) if name in allowed]
            except (KeyError, TypeError, ValueError):
                raise User_Error(
                    PERSISTED_DATA_UNREADABLE, "Stored import metadata could not be read.",
                    "Remove this import and re-import the original files.",
                    file_name=", ".join(record.source_files),
                ) from None
            points.extend(imported)
            rows.extend(daily)
        return points, rows

    def _attach(self, session: SleepSession):
        points, rows = self._load_points(session)
        attached = attach_telemetry(session, points)
        hrv = None if any(p.metric is Metric.hrv_rmssd for p in attached.telemetry) else session_hrv_for(
            attached, rows, self.display_tz)
        return replace(attached, session_hrv=hrv), rows

    def _discover(self, report: Import_Report, selection: Selection | None = None):
        # Always reload every persisted import, even when no session is available.
        self._load_points()
        resolution = dedup_and_merge(
            [s for _, s in self.metadata_store.list_candidate_sessions()], report, self.display_tz)
        candidates = sorted(resolution.sessions, key=lambda s: s.start_time, reverse=True)
        current = selection or self.metadata_store.get_selected_session()
        if not candidates and current is None:
            return candidates, None
        chosen = resolve_selection(candidates, current, self.display_tz)
        attached, rows = self._attach(chosen.session)
        add_missing_metric_warnings(report, attached, attached.telemetry, rows, self.display_tz)
        return candidates, Selection(attached, chosen.kind)

    def _publish(self, candidates, selection):
        self.candidates = candidates
        self.selected_session = None if selection is None else selection.session

    def _import_batch(self, requests):
        written = []
        try:
            with self.metadata_store.transaction():
                results = []
                for source_id, files, overrides in requests:
                    import_id = uuid.uuid4().hex
                    written.append(import_id)
                    results.append(self.registry.run_import(
                        source_id, files, overrides, self.display_tz, self.stores, import_id=import_id))
                report = merge_import_reports(r.report for r in results)
                warning_count = len(report.warnings)
                discarded_count = report.discarded_duplicate_sessions
                candidates, selection = self._discover(report)
                if selection is not None:
                    self.metadata_store.set_selected_session(selection.session, selection.kind)
                # Each persisted report describes its own source. The caller of a
                # sample batch receives the combined report for both sources.
                saved_report = merge_import_reports([results[-1].report])
                saved_report.warnings.extend(report.warnings[warning_count:])
                saved_report.discarded_duplicate_sessions += report.discarded_duplicate_sessions - discarded_count
                self.metadata_store.update_import_report(results[-1].import_id, saved_report)
        except BaseException:
            for import_id in written:
                self.telemetry_store.delete(import_id)
            raise
        self._publish(candidates, selection)
        if selection is None:
            raise no_sleep_session_error()
        return report, candidates

    def import_files(self, source_id: str, files: Source, tz_overrides: Mapping[str, str] | None = None):
        return self._import_batch([(source_id, files, tz_overrides)])

    def import_sources(self, requests):
        """Atomically import (source_id, files, timezone_overrides) groups."""
        if not requests:
            raise User_Error(SOURCE_LOAD_FAILED, "No input files were provided.", "Choose at least one local export file.")
        return self._import_batch(requests)

    def use_sample_data(self):
        directory = Path(__file__).resolve().parents[2] / "sample_data"
        fitbit = sorted(p for p in directory.iterdir() if p.name != "sensorpush.csv")
        overrides = {ft: SAMPLE_TIMEZONE for ft in (
            FITBIT_SLEEP, FITBIT_HRV_DETAILS, FITBIT_HRV_SUMMARY, FITBIT_HEART_RATE, FITBIT_STEPS)}
        return self._import_batch([
            ("fitbit", fitbit, overrides),
            ("sensorpush", [directory / "sensorpush.csv"], {SENSORPUSH: SAMPLE_TIMEZONE}),
        ])

    def _set_selection(self, selection: Selection):
        report = Import_Report()
        with self.metadata_store.transaction():
            candidates, selected = self._discover(report, selection)
            self.metadata_store.set_selected_session(selected.session, selected.kind)
        self._publish(candidates, selected)
        return selected.session

    def select_session(self, choice: str | SleepSession):
        return self._set_selection(choose_session(self.candidates, choice))

    def define_manual_range(self, start, end):
        return self._set_selection(define_manual_session(start, end, self.display_tz))

    def process(self, target_duration: int | None = None, *, mapping_config=None) -> ProcessResult:
        candidates, selection = self._discover(Import_Report())
        if selection is None:
            raise no_sleep_session_error()
        session = selection.session
        if not session.has_stage_data and not any(
            p.metric in (Metric.heart_rate, Metric.temperature, Metric.humidity, Metric.pressure)
            for p in session.telemetry
        ):
            raise User_Error(
                INSUFFICIENT_DATA, "The selected session has no sleep stages, heart rate, or room measurements.",
                "Import data for this night or choose another sleep session.",
            )
        target = select_target_duration(target_duration, mapping_config)
        compression = night_compression(session, target)
        nearby, _ = self._load_points()
        zones = [r.import_report.applied_source_timezones.get(FITBIT_HEART_RATE)
                 for r in self.metadata_store.list_imports()]
        timeline, alignment_report = align(session, nearby, heart_rate_source_timezone=next(
            (z for z in reversed(zones) if z), "UTC"))
        smoothing_windows = None
        if mapping_config is not None:
            from datetime import timedelta
            from backend.domain.mapping import FEATURE_METRICS, validate_mapping
            validate_mapping(mapping_config)
            smoothing_windows = {metric: timedelta(minutes=mapping_config.metrics[key].smoothing_minutes)
                                 for key, metric in FEATURE_METRICS.items()}
        features, states, feature_report = extract(timeline, session, target, smoothing_windows=smoothing_windows)
        events = detect(session, timeline, features, states, target, self.display_tz)
        return ProcessResult(session, timeline, features, tuple(states), tuple(events),
                             merge_processing_reports([alignment_report, feature_report]),
                             compression, input_fingerprint(session))

    def generate(self, config=None, *, seed=None, output=None):
        """Process the selected session and atomically store its WAV and manifest."""
        from backend.domain.mapping import DEFAULT_MAPPING, validate_seed
        from backend.sonification.generation import generate_replay
        config = DEFAULT_MAPPING if config is None else config
        validate_seed(config.random_seed if seed is None else seed)
        try:
            result = self.process(mapping_config=config)
        except User_Error as error:
            if error.code == INSUFFICIENT_DATA:
                raise User_Error("NO_USABLE_DATA", "The session contains no usable data.",
                                 "Import Fitbit sleep or heart-rate files, or a SensorPush CSV for this night.") from None
            raise
        return generate_replay(result.session, result.timeline, result.features, result.coarse_states, result.events,
            data_dir=self.data_dir, metadata_store=self.metadata_store, config=config, seed=seed, output=output,
            display_timezone=self.display_tz.key, processing_report=result.report, input_fingerprint=result.input_fingerprint)
