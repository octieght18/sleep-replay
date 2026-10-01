# Command reference

Run commands from the clone root with its virtual environment Python. Installation and startup are in the [README](../README.md). `python -m backend.api.cli --help` and `generate --help` list options. Successful CLI operations exit 0; structured user/data errors exit 1; invalid argparse syntax exits 2. Error output includes code, description, action and file name where applicable.

## `sample-data`

```bash
python -m backend.api.cli sample-data --out synthetic-night --seed 20240301
```

`--out PATH` is required. `--seed INTEGER` defaults to 20240301 and controls deterministic synthetic generation. The equivalent generator entry point is `python -m tools.sample_data_generator --out synthetic-night --seed 20240301`. Files describe eight hours in `America/New_York`, March 1–2, 2024, and contain no real person's measurements.

## `generate`

```bash
python -m backend.api.cli generate export.zip room.csv --target-duration 180 --output night.wav
```

| Argument/option | Meaning/default |
|---|---|
| `paths...` | Required local Fitbit JSON/CSV, one Fitbit ZIP, and/or SensorPush CSV files; no shell wildcard needed |
| `--config PATH` | UTF-8 YAML/JSON mapping; omitted keys use backend defaults; absent option uses all defaults |
| `--target-duration INTEGER` | 30, 120, 180, 300 or 600 seconds; config/default 180 otherwise |
| `--seed INTEGER` | 0–4294967295; config/default 20240301 otherwise |
| `--display-timezone IANA_ZONE` | Display zone, otherwise `SLEEP_REPLAY_DISPLAY_TIMEZONE`, host zone, then UTC fallback |
| `--source-timezone FILE_TYPE=IANA_ZONE` | Repeatable override for one of the six file types listed below |
| `--session-date YYYY-MM-DD` | Select a session by its end date in the display zone; default detector auto-selection |
| `--manual-start DATETIME`, `--manual-end DATETIME` | Both required together; range at most 24 hours, cannot combine with session date; offset-free times use display zone |
| `--output PATH.wav` | Also export WAV and same-stem JSON; replaces existing pair atomically; default only stores under data directory |
| `--data-dir PATH` | Override `SLEEP_REPLAY_DATA_DIR` and platform default |

Each file type has a dedicated override too: `--fitbit-sleep-timezone`, `--fitbit-heart-rate-timezone`, `--fitbit-steps-timezone`, `--fitbit-hrv-details-timezone`, `--fitbit-hrv-summary-timezone`, `--sensorpush-timezone`. They correspond to `fitbit_sleep`, `fitbit_heart_rate`, `fitbit_steps`, `fitbit_hrv_details`, `fitbit_hrv_summary`, `sensorpush`. Dedicated options take precedence over repeated generic options; explicit input timestamp offsets take precedence over both. Heart rate/steps default to UTC; the other four default to the display zone.

CLI duration/seed override the mapping file. A command uses only its supplied files and configuration; persisted browser settings/imports do not affect the result. On success it prints paths, selected session, compression ratio, event count and warnings. Earlier replays and destination files are preserved on failure.

Synthetic example, with the required sample intraday timezone overrides:

```bash
python -m backend.api.cli generate sample_data/sleep-2024-03-01.json sample_data/heart_rate-2024-03-01.json sample_data/steps-2024-03-01.json "sample_data/Heart Rate Variability Details - 2024-03-01.csv" sample_data/sensorpush.csv --config mapping-config.example.yaml --display-timezone America/New_York --fitbit-heart-rate-timezone America/New_York --fitbit-steps-timezone America/New_York --output night.wav
```

Room readings without a sleep log:

```bash
python -m backend.api.cli generate room.csv --display-timezone America/New_York --manual-start 2024-03-01T22:00 --manual-end 2024-03-02T06:00 --target-duration 30 --output room-night.wav
```

## App servers

`python -m backend.api.run` starts both servers and exits on Ctrl+C. `python -m backend.api.app` starts only the API on 8735; `python -m backend.api.static_server` starts only the frontend on 8734. Each uses its host override from [security-and-storage.md](security-and-storage.md), checks Python >=3.11, and fails with the specific port if already occupied. They do not silently select different ports. Startup failure exits nonzero.
