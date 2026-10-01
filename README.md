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
  sonification/ # mapping config → sound parameters (planned)
  audio/        # WAV rendering (planned)
  api/          # backend API for the browser UI (planned)
tests/          # pytest + Hypothesis test suite
plan/specs/     # full requirements/design/tasks specs (see below)
sample_data/    # synthetic sample dataset
tools/          # deterministic sample-data generator
```

## Setup

Requires **Python 3.11+**.

```powershell
python -m venv .venv
.venv\Scripts\activate
python -m pip install -r requirements.txt
```

## Running the tests

From the repository root:

```powershell
python -m pytest
```

The suite uses pytest with property-based tests via Hypothesis and covers the
domain model, importers, timezone handling, alignment, compression, feature
extraction, event detection, and persistence.

## Try the data pipeline

The data pipeline is available as a local Python service. HTTP, audio rendering,
and the browser UI belong to the other specs and remain planned.

```python
from backend.api.pipeline import Pipeline

with Pipeline() as pipeline:
    report, candidates = pipeline.use_sample_data()
    result = pipeline.process(180)
    print(len(candidates), len(result.features), len(result.events))  # 1, 360, 12
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
reading input. Import and processing require no credentials or network access.

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
| `sleep-replay-sonification` | Mapping config, soundscape presets, audio rendering, replay manifest, CLI | Planned |
| `sleep-replay-app` | Backend API, browser UI, Docker setup, end-to-end flow | Planned |

Each spec contains `requirements.md`, `design.md`, and `tasks.md` with the full
EARS-style acceptance criteria and design decisions.

## Non-goals (MVP)

No accounts, no cloud, no live wearable/API integration, no AI/LLM analysis,
no medical interpretation, no alarms, no mobile apps, and WAV output only.
Data-source adapters for other wearables are a documented future extension
point via the `Data_Source_Adapter` interface.

## License

Released under the [MIT License](LICENSE).
