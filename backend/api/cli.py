"""Local sample-data and generate commands: python -m backend.api.cli --help."""
from __future__ import annotations

import argparse
import sys
import tempfile
import uuid
from dataclasses import replace
from datetime import date, timezone
from pathlib import Path

from backend.api.pipeline import Pipeline
from backend.domain.errors import User_Error
from backend.domain.mapping import DEFAULT_MAPPING, validate_seed
from backend.domain.sample_dataset import SAMPLE_RANDOM_SEED
from backend.domain.timezones import FILE_TYPES, validate_iana
from backend.ingestion.fitbit.packaging import match_fitbit_name
from backend.persistence.data_directory import prepare_data_dir
from backend.persistence.metadata_store import MetadataStore
from backend.persistence.safe_logging import configure_logging, get_logger, shutdown_logging
from backend.processing.compression import validate_target_duration
from backend.processing.session_detector import auto_select
from backend.sonification.config_parser import MAX_CONFIG_BYTES, parse_config
from backend.sonification.generation import atomic_artifacts, generate_replay
from tools.sample_data_generator import dataset_bytes, generate_night


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise User_Error("SOURCE_LOAD_FAILED", "Invalid command-line arguments: " + message, "Run python -m backend.api.cli --help, or the command followed by --help.")


def parser():
    root = _Parser(description="Generate an ambient audio replay from local sleep and room exports.")
    commands = root.add_subparsers(dest="command", required=True, parser_class=_Parser)
    sample = commands.add_parser("sample-data", help="Write the deterministic synthetic night")
    sample.add_argument("--out", type=Path, required=True)
    sample.add_argument("--seed", type=int, default=SAMPLE_RANDOM_SEED)
    generate = commands.add_parser("generate", help="Import local files and render WAV plus manifest")
    generate.add_argument("paths", type=Path, nargs="+")
    generate.add_argument("--config", type=Path)
    generate.add_argument("--target-duration", type=int)
    generate.add_argument("--seed", type=_seed_option)
    generate.add_argument("--sound-style", choices=("music", "nature"), help="Sound character; config/default music otherwise")
    generate.add_argument("--display-timezone", help="IANA zone; otherwise the configured/host zone")
    generate.add_argument("--source-timezone", action="append", default=[], metavar="FILE_TYPE=IANA_ZONE")
    for file_type in FILE_TYPES:
        generate.add_argument("--" + file_type.replace("_", "-") + "-timezone", dest=file_type + "_timezone")
    generate.add_argument("--session-date", help="YYYY-MM-DD in the display timezone")
    generate.add_argument("--manual-start")
    generate.add_argument("--manual-end")
    generate.add_argument("--output", type=Path, help="WAV destination; JSON manifest uses the same stem")
    generate.add_argument("--data-dir", type=Path, help="Override SLEEP_REPLAY_DATA_DIR")
    return root


def _seed_option(value):
    try:
        return validate_seed(int(value))
    except ValueError:
        raise User_Error("INVALID_RANDOM_SEED", "The random seed is invalid.", "Use an integer from 0 to 4294967295.") from None


def _generate(args):
    config = DEFAULT_MAPPING
    if args.config:
        try:
            with args.config.open("rb") as stream:
                config = parse_config(stream.read(MAX_CONFIG_BYTES + 1))
        except OSError:
            raise User_Error("INVALID_MAPPING_CONFIG", "The mapping configuration file cannot be read.",
                             "Choose a readable UTF-8 YAML or JSON configuration file.", file_name=args.config.name) from None
    if args.seed is not None:
        config = replace(config, random_seed=validate_seed(args.seed))
    if args.target_duration is not None:
        config = replace(config, target_duration=validate_target_duration(args.target_duration))
    if args.sound_style is not None:
        config = replace(config, sound_style=args.sound_style)
    if args.session_date and (args.manual_start or args.manual_end):
        raise User_Error("INVALID_MANUAL_RANGE", "A session date cannot be combined with manual times.", "Choose --session-date or both --manual-start and --manual-end.")
    if bool(args.manual_start) != bool(args.manual_end):
        raise User_Error("INVALID_MANUAL_RANGE", "Both manual times are required.", "Provide --manual-start and --manual-end together.")
    wanted_date = None
    if args.session_date:
        try:
            wanted_date = date.fromisoformat(args.session_date)
        except ValueError:
            raise User_Error("NO_SLEEP_SESSION", "The session date is invalid.", "Use YYYY-MM-DD.") from None
    if args.display_timezone:
        validate_iana(args.display_timezone)
    overrides = {}
    for specification in args.source_timezone:
        kind, separator, name = specification.partition("=")
        if not separator or kind not in FILE_TYPES:
            raise User_Error("INVALID_TIMEZONE", "The source-timezone option is invalid.", "Use FILE_TYPE=IANA_ZONE; file types: " + ", ".join(FILE_TYPES))
        overrides[kind] = name
    for kind in FILE_TYPES:
        name = getattr(args, kind + "_timezone")
        if name is not None:
            overrides[kind] = name
    for kind, name in overrides.items():
        validate_iana(name, kind)
    grouped = {"fitbit": [], "sensorpush": []}
    for path in args.paths:
        if not path.is_file():
            raise User_Error("SOURCE_LOAD_FAILED", "An input file is missing or unreadable.", "Choose a readable local export file.", file_name=path.name)
        source = "sensorpush" if path.suffix.lower() == ".csv" and match_fitbit_name(path.name) is None else "fitbit"
        grouped[source].append(path)
    requests = [(source, sorted(paths), overrides) for source, paths in grouped.items() if paths]
    directory = prepare_data_dir(None if args.data_dir is None else {"SLEEP_REPLAY_DATA_DIR": str(args.data_dir)})
    # Each invocation uses exactly its input files. Prior imports/settings cannot
    # change its result; all transient telemetry remains inside the Data_Directory.
    with tempfile.TemporaryDirectory(prefix=".cli-", dir=directory) as staging:
        with Pipeline(staging, display_timezone=args.display_timezone) as pipeline:
            try:
                pipeline.import_sources(requests)
            except User_Error as error:
                if error.code != "NO_SLEEP_SESSION" or not args.manual_start:
                    raise
            if args.manual_start:
                pipeline.define_manual_range(args.manual_start, args.manual_end)
            elif wanted_date:
                candidates = [s for s in pipeline.candidates if s.end_time.astimezone(pipeline.display_tz).date() == wanted_date]
                if not candidates:
                    dates = sorted({s.end_time.astimezone(pipeline.display_tz).date().isoformat() for s in pipeline.candidates})
                    raise User_Error("NO_SLEEP_SESSION", f"No sleep session ends on {wanted_date}.", "Choose an available end date: " + ", ".join(dates))
                pipeline.select_session(auto_select(candidates, pipeline.display_tz))
            try:
                processed = pipeline.process(mapping_config=config)
            except User_Error as error:
                if error.code == "INSUFFICIENT_DATA":
                    raise User_Error("NO_USABLE_DATA", "The session contains no usable data.", "Import Fitbit sleep or heart-rate files, or a SensorPush CSV.") from None
                raise
            with MetadataStore(directory) as store:
                result = generate_replay(processed.session, processed.timeline, processed.features, processed.coarse_states,
                    processed.events, data_dir=directory, metadata_store=store, config=config,
                    display_timezone=pipeline.display_tz.key, processing_report=processed.report,
                    input_fingerprint=processed.input_fingerprint, output=args.output)
    print(result.wav_path)
    print(result.manifest_path)
    print(f"Session: {result.manifest.session_start.isoformat()} to {result.manifest.session_end.isoformat()}")
    print(f"Compression ratio: {result.manifest.compression_ratio:.1f}")
    print(f"Night events: {len(result.manifest.night_events)}")
    for warning in result.manifest.warnings:
        print(f"Warning: {warning}")


def main(argv=None):
    try:
        args = parser().parse_args(argv)
        if args.command == "sample-data":
            artifacts = {args.out.expanduser().absolute() / name: content for name, content in dataset_bytes(generate_night(args.seed)).items()}
            try:
                with atomic_artifacts(artifacts):
                    pass
            except OSError:
                raise User_Error("SAVE_FAILED", "The sample files could not be written.", "Choose a writable output directory.", file_name=str(args.out)) from None
            for path in artifacts:
                print(path)
        else:
            _generate(args)
        return 0
    except User_Error as error:
        print(str(error), file=sys.stderr)
        return 2
    except Exception as error:
        reference = uuid.uuid4().hex[:12]
        try:
            data_dir = prepare_data_dir(None if getattr(args, "data_dir", None) is None else {"SLEEP_REPLAY_DATA_DIR": str(args.data_dir)})
            configure_logging(data_dir)
            get_logger("cli").exception("command.failed", error, reference_id=reference, error_code="RENDERING_FAILED")
        except Exception:
            pass
        finally:
            shutdown_logging()
        print(f"[RENDERING_FAILED] An internal failure stopped the command. Report reference {reference} and try again.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
