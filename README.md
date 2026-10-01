# Sleep Replay

Sleep Replay turns one night of sleep and bedroom-environment telemetry into a
short ambient audio composition (30 seconds to 10 minutes, default 3 minutes).

After waking, you import a Fitbit export and a SensorPush CSV. The application
discovers the night's sleep session, aligns both datasets on a common timeline,
extracts features and events, maps them to a small set of interacting musical
dimensions, and renders a WAV file you can listen to — a compressed auditory
replay of your night.

> **Example:** 8 hours of telemetry → normalize and align → extract features
> and events over time windows → map to sound parameters → render ~3 minutes
> of audio (160× temporal compression).

**Sleep Replay is an aesthetic auditory replay. It is not a medical device,
not a diagnostic tool, and not an alarm. It provides no diagnosis or health
advice.**

## Design principles

- **Local-first.** Single user, no sign-in, no cloud. Imported files, sessions,
  settings, and replays never leave your machine, and the app works fully
  offline once installed.
- **One coherent composition.** Every metric shapes a small set of interacting
  musical dimensions of shared sound layers, so the replay sounds like an
  intentional ambient piece — not a series of beeps per sensor.
- **Replay, not raw data.** Audio is generated from extracted features and
  events only; raw telemetry never reaches the audio output.

## Data inputs

| Source | Format | Contents |
|---|---|---|
| **Fitbit export** (Google Takeout / Fitbit account export) | Individual `.json` / `.csv` files or one `.zip` archive | Sleep logs (stages or classic), intraday heart rate, HRV (RMSSD), per-minute steps |
| **SensorPush CSV** (app or web dashboard export) | One or more `.csv` files (comma or semicolon delimited) | Temperature, relative humidity, barometric pressure |

Files are matched by name (case-insensitive). Timestamps without an explicit
offset use a per-file-type default source timezone (overridable at import);
temperature/pressure units are inferred when headers don't state them.
All other files and columns are ignored and reported as skipped.

## How it works

1. **Import** — Fitbit and SensorPush files are normalized into timestamped
   telemetry points with canonical units.
2. **Session discovery** — the night's sleep session is detected, deduplicated,
   and merged from the sleep logs.
3. **Alignment** — both sources are resampled onto a common aligned timeline,
   with timezone-mismatch warnings when the data overlap looks wrong.
4. **Features & events** — per-window features, coarse sleep states
   (deep / light / REM / wake / restless), and night events are extracted.
5. **Sonification** — features are mapped through soundscape presets onto
   interacting musical dimensions (pulse, brightness, texture, transients…)
   and rendered to a deterministic WAV file with a replay manifest.
6. **Playback** — a minimal browser UI shows a synchronized timeline with a
   moving playhead, the current coarse sleep state, and event markers.

## Repository layout

```
backend/
  domain/       # normalized telemetry, sessions, stages, units, errors
  ingestion/    # Fitbit + SensorPush importers, source-adapter registry
  processing/   # session detection, alignment, resampling, compression,
                # feature extraction, event detection
  persistence/  # data directory, metadata + telemetry stores, safe logging
  sonification/ # mapping configuration, render plans, manifests, generation
  audio/        # deterministic stereo PCM WAV synthesis
  api/          # pipeline service and CLI; HTTP API, settings, replay cache, local startup
tests/          # pytest + Hypothesis test suite
plan/specs/     # full requirements/design/tasks specs (see below)
sample_data/    # synthetic sample dataset
tools/          # deterministic sample-data generator
examples/       # playable sample WAV and its replay manifest
docs/           # user, format, architecture, mapping, CLI and troubleshooting guides
```

## Run locally (Docker is optional)

Requires **Python 3.11+**, Git to clone, and a modern browser. There is no Node.js
requirement or frontend build step. Network access is only needed to clone and
install dependencies; the installed app runs offline.

Windows PowerShell, without activation or administrator privileges:

```powershell
git clone https://github.com/octieght18/sleep-replay.git
cd sleep-replay
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m backend.api.run
```

Use `py -3.11` instead if multiple installed versions need disambiguation. On
macOS/Linux:

```bash
git clone https://github.com/octieght18/sleep-replay.git
cd sleep-replay
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m backend.api.run
```

Open **http://127.0.0.1:8734**. Choose **Use sample data**, **Open replay**,
**Generate replay**, then **Play**. Settings save locally; generating loads at
00:00 without autoplay. Press Ctrl+C in the terminal to stop both servers.
The API listens on 127.0.0.1:8735. Startup reports occupied ports and Python
versions below 3.11 instead of silently selecting other ports.

To start each server in a separate PowerShell terminal after installation:

```powershell
.\.venv\Scripts\python.exe -m backend.api.app
```

```powershell
.\.venv\Scripts\python.exe -m backend.api.static_server
```

Their macOS/Linux equivalents use `.venv/bin/python` with the same `-m` modules.
All commands below use `python` from an activated virtual environment, or you
can substitute the explicit virtual environment executable shown above.

## Optional Docker setup

Requires Docker Engine/Desktop with Docker Compose v2. From the clone root:

```bash
docker compose up --build
```

Open the same **http://127.0.0.1:8734** and follow the same sample-data steps.
Images are exact-version, digest-pinned official Python images from the public
ECR mirror. Published ports are loopback-only. Docker defaults the display
zone to UTC; set `SLEEP_REPLAY_DISPLAY_TIMEZONE` before starting to override it.
The named data volume persists across:

```bash
docker compose down
docker compose up
```

Do not start both variants together on the same ports. Deletion and exposure
controls are documented in [security and storage](docs/security-and-storage.md).

## Guides

- [Export/import and playback](docs/user-guide.md)
- [Accepted data formats and timezones](docs/data-formats.md)
- [Architecture and new source adapters](docs/architecture.md)
- [Mapping configuration](docs/mapping-config.md) and [commented example](mapping-config.example.yaml)
- [CLI reference](docs/cli.md)
- [Local data and security](docs/security-and-storage.md)
- [Troubleshooting every error code and warning](docs/troubleshooting.md)

## Running the tests

From the repository root:

```powershell
python -m pytest
```

The suite uses pytest with property-based tests via Hypothesis and covers the
domain model, importers, timezone handling, alignment, compression, feature
extraction, event detection, persistence, configuration, audio synthesis,
replay manifests, determinism, graceful degradation, CLI, API, settings, cache,
and frontend display logic (embedded JavaScript engine, no Node required). All tests
write to temporary directories and run offline. Timed generation checks are
marked `reference_machine`; on slower hardware, deselect them with
`python -m pytest -m "not reference_machine"`.

Optional Chromium DOM/playback checks (free ports 8734/8735 first):

```bash
python -m pip install -r requirements-browser.txt
python -m playwright install chromium
python -m pytest --run-browser
```

These browser tests are skipped by the standard command; API and pure frontend
logic tests always run. Set `SLEEP_REPLAY_BROWSER` to an installed Chromium
executable to use it instead of downloading a browser.

## Generate an audio replay

Listen to [the three-minute example WAV](examples/example_replay.wav). Its
[manifest](examples/example_replay.json) records the session, sleep states,
events, windowed environmental values, availability, and effective settings.
It contains no raw telemetry or generation timestamp.

From the repository root, this command regenerates both example files
byte-for-byte. The target duration is **180 seconds**, random seed **20240301**,
and display timezone **America/New_York**. The command works in PowerShell and
Bash; explicit file names keep it independent of shell wildcard expansion.

```powershell
python -m backend.api.cli generate "sample_data/sleep-2024-03-01.json" "sample_data/heart_rate-2024-03-01.json" "sample_data/steps-2024-03-01.json" "sample_data/Heart Rate Variability Details - 2024-03-01.csv" "sample_data/sensorpush.csv" --target-duration 180 --seed 20240301 --display-timezone America/New_York --fitbit-heart-rate-timezone America/New_York --fitbit-steps-timezone America/New_York --output examples/example_replay.wav
```

Use your own Fitbit JSON/CSV files or one Fitbit ZIP, plus any SensorPush CSVs,
as positional arguments. Durations are 30, 120, 180, 300, or 600 seconds. For real
Fitbit intraday exports, heart rate and steps default to UTC; the overrides above
are specific to the synthetic dataset. Other file types default to the display
timezone. An explicit offset in an input timestamp takes precedence.

`--config docs/mapping-config.example.yaml` loads a YAML or JSON mapping;
`--target-duration` and `--seed` override that file. Omitted settings use defaults,
and the CLI ignores previously stored settings and imports. The six metric keys
map to shared sound parameters with sensitivity, smoothing, and hysteresis.
The [example configuration](docs/mapping-config.example.yaml) includes all defaults.
An invalid configuration applies no values and produces no replay.

Use `--session-date YYYY-MM-DD` to select by the session's end date. For room data
without sleep logs, provide `--manual-start 2024-03-01T22:00:00` and
`--manual-end 2024-03-02T06:00:00`; offset-free times use the display timezone.
Manual times cannot be combined with a session date. Override other source
timezones with `--source-timezone FILE_TYPE=IANA_ZONE`; `generate --help` lists
individual timezone options as well.

Successful generation prints absolute WAV/JSON paths, session times, compression
ratio, event count, and warnings. Each replay is stored under the Data_Directory's
`replays/` folder with a SQLite record before completion is reported. `--output`
also exports the WAV and a JSON file with the same stem, replacing existing
destination files together; a failed generation preserves those files and
earlier replays. Generation time belongs only to the SQLite record.

```powershell
python -m backend.api.cli sample-data --out synthetic-night
python -m backend.api.cli generate --help
```

## Try the data pipeline

The pipeline and audio generation are available as a local Python service.
The HTTP API and browser UI wrap the same local service.

```python
from backend.api.pipeline import Pipeline

with Pipeline() as pipeline:
    report, candidates = pipeline.use_sample_data()
    result = pipeline.process(180)
    print(len(candidates), len(result.features), len(result.events))  # 1, 360, 12
    replay = pipeline.generate()
    print(replay.wav_path, replay.manifest_path)
```

`Pipeline.import_files(source_id, files, tz_overrides)` imports real local exports.
The built-in source identifiers are `fitbit` and `sensorpush`. Register additional
adapters through `pipeline.registry.register(adapter)`. Use `select_session()` to
choose a candidate or `define_manual_range(start, end)` to define a session.
Imports and explicit selections survive restart. An import without a sleep log
raises `NO_SLEEP_SESSION` while keeping its telemetry for a later manual range.

Set `SLEEP_REPLAY_DATA_DIR` to choose where telemetry and SQLite records are
stored. Otherwise the default is `%LOCALAPPDATA%\SleepReplay` on Windows,
`~/Library/Application Support/SleepReplay` on macOS, or
`$XDG_DATA_HOME/sleep-replay` on Linux (falling back to
`~/.local/share/sleep-replay`). Set `SLEEP_REPLAY_DISPLAY_TIMEZONE` to an IANA
name such as `America/New_York`; the default is the host timezone, with UTC
and a warning when it cannot be determined. Startup checks storage before
reading input. Import, processing, and audio generation require no credentials
or network access. `Pipeline(display_timezone="America/New_York")` sets an
explicit timezone without changing the process environment.

## Synthetic sample data

The files in `sample_data/` were produced by `tools.sample_data_generator`, are
unrelated to any real person, and describe an eight-hour night in
`America/New_York` on March 1–2, 2024, with no daylight saving transition.
The documented seed is `20240301`. Regenerate byte-identical files with:

```powershell
python -m tools.sample_data_generator --seed 20240301 --out sample_data
```

The generator validates sleep cycles, awakenings, heart rate and HRV, movement
bursts and restless periods, environmental changes, and missing-data gaps.
All synthetic timestamps are written in the sample timezone, including Fitbit
heart rate and steps. `use_sample_data()` supplies the required timezone
overrides independently of the host timezone. For manual sample imports, use
`America/New_York` for all sample file types, including `fitbit_heart_rate`
and `fitbit_steps` whose usual default for real Fitbit exports is UTC.

## Project status

This repository is being built from three specs (in `plan/specs/`):

| Spec | Scope | Status |
|---|---|---|
| `sleep-replay-data-pipeline` | Domain model, import, session discovery, alignment, features, events, persistence | Implemented |
| `sleep-replay-sonification` | Mapping config, soundscape presets, audio rendering, replay manifest, CLI | Implemented |
| `sleep-replay-app` | Backend API, browser UI, optional Docker setup, end-to-end flow | Implemented |

Each spec contains `requirements.md`, `design.md`, and `tasks.md` with the full
EARS-style acceptance criteria and design decisions.

## Non-goals (MVP)

No accounts, no cloud, no live wearable/API integration, no AI/LLM analysis,
no medical interpretation, no alarms, no mobile apps, and WAV output only.
Data-source adapters for other wearables are a documented future extension
point via the `Data_Source_Adapter` interface.

## License

Released under the [MIT License](LICENSE).
