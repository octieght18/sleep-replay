# Troubleshooting

Errors show a description, an action and the affected filename where available. Unexpected errors also show a reference ID; include it when reporting the failure. Logs contain operational metadata rather than raw measurements.

| Code | Cause | Action |
|---|---|---|
| `NO_FITBIT_FILES` | No names match supported Fitbit patterns | Export the required Takeout data types or select the supported individual files listed in data-formats.md |
| `MULTIPLE_ARCHIVES` | More than one Fitbit archive or an incompatible archive/file combination | Choose one ZIP or individual Fitbit files |
| `ARCHIVE_UNREADABLE` | ZIP cannot be opened or is damaged | Download/export again, or extract supported files with a local ZIP tool |
| `NO_TIMESTAMP_COLUMN` | CSV lacks a timestamp column or any measurement column | Export a header row with date/time and temperature, humidity or pressure |
| `NO_READABLE_ROWS` | CSV is empty or yields no valid measurements | Check timestamps, numeric values, delimiter and units; export again |
| `INVALID_TIMEZONE` | Unknown file-type key or invalid IANA zone | Use a listed file type and an IANA identifier such as America/New_York |
| `SESSION_TOO_SHORT` | Session is not longer than the requested replay duration | Choose a shorter duration or a longer session |
| `INVALID_TARGET_DURATION` | Duration is outside the five supported choices | Use 30, 120, 180, 300 or 600 seconds |
| `NO_SLEEP_SESSION` | No sleep logs/candidate selection, unavailable date or no selected session | Import sleep logs, choose an available end date, or enter a manual range after importing telemetry |
| `INVALID_MANUAL_RANGE` | Missing/unparseable times, end before start, duration above 24 hours or conflicting CLI options | Enter both times in the display zone, end after start, at most 24 hours, without --session-date |
| `SAVE_FAILED` | Import or metadata cannot be saved | Check directory permissions and free space; retry |
| `PERSISTED_DATA_UNREADABLE` | Previously stored data is missing or corrupt | Stop the app, restore a backup or move the affected data directory aside and reimport |
| `DATA_DIR_UNWRITABLE` | Startup cannot create/write its directory | Choose a writable SLEEP_REPLAY_DATA_DIR and restart |
| `INSUFFICIENT_DATA` | Selected range has no stage, heart rate or environmental data | Import measurements covering that range or choose another night |
| `SOURCE_LOAD_FAILED` | Missing/unreadable files, unsupported browser extension, malformed request, invalid filename, upload limit, unknown URL or blocked origin | Follow the displayed action; select JSON/CSV or one ZIP, stay within 2 GiB per file, and open the local frontend URL. For larger input use CLI or extract supported files |
| `INVALID_MAPPING_CONFIG` | Unknown mapping key/target, invalid sensitivity/smoothing/hysteresis/display units or unreadable YAML/JSON | Use mapping-config.example.yaml; browser sensitivities require steps of 0.05, and invalid fields keep the last valid saved setting |
| `INVALID_RANDOM_SEED` | Seed is not an unsigned 32-bit integer | Use an integer from 0 through 4294967295 |
| `NO_USABLE_DATA` | No supported stage, heart rate or environmental data for rendering | Import supported measurements for this night; HRV alone cannot create a replay |
| `RENDERING_FAILED` | Audio validation or unexpected internal operation failure | Retry with defaults; report the reference ID if present when failure recurs |
| `REPLAY_WRITE_FAILED` | WAV/manifest cannot be committed or output extension is wrong | Use a writable .wav destination with free space; earlier files remain intact |
| `GENERATION_IN_PROGRESS` | Another request is already generating | Wait for that request to finish and retry; the first operation continues |

## Nonfatal warnings

| Condition | Meaning and action |
|---|---|
| Unknown sleep level | It is mapped to Unknown; no action required, or check the supported stage names |
| Malformed sleep log or missing start/end | That log is skipped; check/export a valid log |
| Sleep end not after start | Log skipped; check the export's timestamps |
| Stage data absent/unusable | Unknown stage and Neutral preset; import stage records if available |
| Fitbit heart rate, HRV or steps absent | That metric is unavailable; import it for the same night if desired |
| SensorPush temperature/humidity/pressure column missing | Metric unavailable; export the expected column if available |
| Duplicate CSV columns | Leftmost matching column used, others ignored; remove unwanted columns to select another |
| Unit inferred | Header has no recognized unit; verify the inference or add an explicit unit |
| Pressure unit cannot be inferred | Pressure is skipped; label it inHg, kPa, hPa or mbar |
| Unsupported unit during alignment | A measurement cannot be converted and is excluded; correct the source unit |
| Multiple sensors | Sensor with most valid in-session points selected per metric; import only the wanted sensor to change it |
| Overlapping sleep sessions merged | One combined session covers their range; no action required |
| Duplicate sessions/points discarded | Duplicates removed deterministically; no action required, counts appear in reports |
| Invalid rows/values, unsupported files or archive members skipped | Remaining readable data continues; review reasons, missing columns, 200 MiB matching-member cap, encryption or decompression errors |
| Long data gaps | Heart rate gaps above 5 minutes and HRV/environment above 15 minutes stay missing; contributions glide toward neutral; no action required |
| Low heart-rate overlap with nearby data | Source timezone may be wrong; reimport using the zone actually used by the source timestamps |
| Movement derived from stages | Step data absent; movement uses awake/restless stage fraction; no action required |
| Environmental data absent | Replay uses available Fitbit data; import SensorPush readings if desired |
| Fitbit data absent | Environmental replay uses Neutral preset; import sleep/physiology if desired |
| Invalid/missing display zone and unknown host zone | UTC fallback; set SLEEP_REPLAY_DISPLAY_TIMEZONE to an IANA zone before restart |
| Non-loopback host override | No authentication; revert host overrides to 127.0.0.1 to limit exposure |

## Startup and playback

**Backend unreachable:** activate the environment and run `python -m backend.api.run` from the clone root. Reload the browser. The separate-server equivalent is `python -m backend.api.app` plus `python -m backend.api.static_server` in a second terminal.

**Port 8734 or 8735 in use:** stop the earlier Sleep Replay process or the process listening on that exact port. The app reports the occupied port and never moves to another. Do not run the local and Docker variants simultaneously on these ports.

**Python version:** use Python 3.11 or later; startup names an older detected version. Windows users can run `py -3.11 -m venv .venv` if that is the installed supported version. Use the virtual environment executable directly if PowerShell policy prevents activation.

**Audio cannot play/decode:** choose Play again or regenerate; the playhead is kept and previous replay remains available when generation fails. Check that the browser supports PCM WAV and the backend is running. A malformed/out-of-file byte-range request returns a structured 400/416 error.

**Settings could not save:** retry the change after resolving the displayed error. Generation waits for a pending save; it does not silently render unsaved settings.

**Docker image pull/build fails:** network access is required for setup. Images use a digest-pinned official Python image through its public ECR mirror. Retry registry access or use the documented Python setup, which requires no Docker. In a TLS inspection environment provide the build-only proxy_ca secret with the trusted CA bundle; never disable certificate checks.
