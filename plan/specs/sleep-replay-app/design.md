# Design Document

## Overview

The sleep-replay-app spec is the third and final spec of the Sleep Replay MVP. It wraps the data pipeline (import → session → align → features → events) and the sonification engine (features → WAV + Replay_Manifest) in a local HTTP Backend_API and a minimal browser Frontend, and delivers the setup, Docker, privacy, and documentation deliverables that make the project runnable with `docker compose up` or a documented Windows setup.

The end-to-end flow it enables: start the application → import a Fitbit_Export and a SensorPush_CSV (or use the Sample_Dataset) → pick a candidate SleepSession → Generate Replay → Play with a synchronized Playback_Timeline → change mappings in the Settings_Panel and regenerate — all without the CLI, without re-importing, and without any network access beyond the loopback interface.

The design adds two runtime processes:

```
Browser (Frontend, static assets + vanilla JS)
    │  HTTP, loopback only, same-origin
    ▼
Backend_API (FastAPI, 127.0.0.1) ──▶ api/pipeline.py (import, sessions, processing)
    │                                sonification/generation.py (render, cache)
    ▼
Data_Directory (telemetry, Metadata_Store, Replays)  ← persisted across restarts/containers
```

The Frontend derives everything it displays during playback — Coarse_State, Night_Time, environmental values, event markers — exclusively from the loaded Replay's Replay_Manifest (Requirement 4.12), so the API surface needed during playback is just static audio + one JSON document, and playback correctness is testable without a running renderer.

### Design goals and key decisions

| Decision | Rationale |
|---|---|
| FastAPI + uvicorn for the Backend_API, pinned | Mature request validation and multipart handling; pinned exact versions satisfy Requirement 9.5 |
| Static Frontend with no build step (vanilla HTML/CSS/JS), served by a tiny stdlib static server | Requirement 9.3 requires Node.js only where a build step exists; no build step keeps the Windows setup to Python alone and the Docker image small |
| Loopback-only binding by default in both setups, with an explicit opt-in warning | Requirements 1.8, 7.3, 7.5; the API has no authentication |
| Replay caching keyed on (Input_Fingerprint, software version, effective Mapping_Config) | Requirement 5.7: regeneration with unchanged inputs returns the stored Replay within 2 s without rendering |
| Single-flight generation with `GENERATION_IN_PROGRESS` | Requirement 3.10; the renderer is CPU-bound and determinism is easier to reason about with one render at a time |
| Frontend state derived only from the Replay_Manifest | Requirement 4.12; keeps the timeline, markers, and environmental readouts testable and consistent with the audio |
| All API errors are User_Error-shaped JSON; internal errors carry a reference identifier logged with metadata only | Requirements 8.1, 8.2; log content restrictions from sleep-replay-data-pipeline still apply |

## Architecture

### Processes and ports

| Process | Default bind | Port | Serves |
|---|---|---|---|
| Backend_API | 127.0.0.1 | 8735 | JSON API + Replay audio/manifest download |
| Frontend server | 127.0.0.1 | 8734 | Static HTML/CSS/JS |

Both ports are documented in the README. Binding a non-loopback interface is possible only through an explicit environment override and prints a startup warning that the API has no authentication and imported data becomes reachable from other devices on the network (7.5). At startup, each process checks its port before binding and exits with an error identifying the port in use rather than serving elsewhere (9.9). The Backend checks for Python ≥ 3.11 before accepting requests and exits with the detected version otherwise (9.8).

### Docker layout

```
docker-compose.yml          # two services, named volume
backend/Dockerfile          # python:3.11-slim (pinned by digest), installs pinned requirements
frontend/Dockerfile         # python:3.11-slim (pinned by digest), static server only
```

- Both services bind inside the compose network; the published ports map to `127.0.0.1:8735` and `127.0.0.1:8734` only, so connection attempts from other machines fail (9.2).
- The Data_Directory is a named volume mounted at `/data` (`SLEEP_REPLAY_DATA_DIR=/data`), persisting imports, settings, and Replays across `docker compose down` / `up` (9.7); the README documents the volume name and the `docker compose down -v` command that deletes all local data (11.8 of the documentation requirement).
- `.dockerignore` and `.gitignore` exclude the default non-Docker Data_Directory while keeping `sample_data/` and `examples/` tracked (7.4).

### Repository additions

```
backend/
  api/
    app.py                  # FastAPI app factory, middleware, loopback/origin guards
    errors.py               # User_Error → JSON mapping, reference-id logging
    routes_imports.py       # import endpoints (files + sample data)
    routes_sessions.py      # candidates, selection, manual range
    routes_settings.py      # settings get/put + validation
    routes_replays.py       # generate (single-flight + cache), manifest, audio
    settings_service.py     # application-wide settings over the Metadata_Store
    replay_cache.py         # cache key computation + lookup
    static_server.py        # stdlib static file server for the Frontend (non-Docker + image)
    pipeline.py, cli.py     # from earlier specs
frontend/
  index.html                # app shell + non-medical statement
  styles.css
  js/
    api.js                  # fetch wrapper: User_Error shape, unreachable-backend message
    import_view.js          # Requirement 2
    main_screen.js          # Requirements 3, 4 (with timeline.js)
    timeline.js             # Playback_Timeline: playhead, markers, seeking, keyboard
    settings_panel.js       # Requirement 5
    units.js                # Display_Units conversion/formatting
docs/
  architecture.md           # components, data flow, adapter extension guide
  data-formats.md           # confirmed Input Format Assumptions
  mapping-config.md         # mapping reference + final default values
  cli.md                    # CLI reference
  troubleshooting.md        # every User_Error code + warning conditions
  setup-windows.md          # non-Docker PowerShell steps
mapping-config.example.yaml # example config accepted by the parser
README.md                   # prerequisites, both setups, URLs, test command, non-medical note
LICENSE                     # OSI-approved
```

The existing AST dependency test already permits `api → ingestion/processing/sonification/audio/domain`; the Frontend is not a Backend package and stays outside the rules (17.1/17.2 of sleep-replay-data-pipeline).

## Components and Interfaces

### Backend_API (backend/api/app.py + routes)

All endpoints are JSON except audio. Every error response is `{"code", "description", "action", "file_name"?, "reference_id"?}` with HTTP 4xx when the cause is user input or request state and 5xx for unexpected internal errors (8.1). An exception middleware maps `User_Error` directly; unexpected exceptions are logged (metadata only: error details + unique reference identifier, no telemetry values or per-sample timestamps) and returned as a 5xx User_Error carrying the same reference id and a retry/report action (8.2).

| Endpoint | Behavior | Requirements |
|---|---|---|
| `POST /api/imports` | Multipart upload of Fitbit files or one zip plus SensorPush CSVs, with per-file-type Source_Timezone overrides; streams to temp files inside the Data_Directory, runs the registry import, returns the Import_Report + candidate list | 2.8, 3.x flow |
| `POST /api/imports/sample` | Imports the committed Sample_Dataset with Sample_Timezone defaults | 2.3, 6.1, 9.4 |
| `GET /api/sessions` | Lists candidates (Session_Detector order, start/end in Display_Timezone, duration, stage-data availability) + the selected session | 8.1 pipeline, 3.4 |
| `PUT /api/sessions/selected` | Selects a candidate by id (user choice) | 8.4 pipeline |
| `POST /api/sessions/manual` | Creates/validates a manual range in Display_Timezone; `INVALID_MANUAL_RANGE` keeps the current selection | 8.8/8.9 pipeline, 3.14 |
| `GET /api/settings` / `PUT /api/settings` | Reads / validates / persists the application-wide settings (Target_Duration, per-metric targets + sensitivities, Random_Seed, Display_Units) | 5.1, 5.3, 5.9, 5.10 |
| `POST /api/replays` | Generates a Replay for the selected session from current settings; single-flight; cache hit returns the stored Replay | 3.5, 3.10, 5.2, 5.7 |
| `GET /api/replays/{id}/manifest` | The Replay_Manifest JSON | 4.x |
| `GET /api/replays/{id}/audio` | The WAV file (range requests for seeking) | 4.9 |

Uploads larger than Max_Upload_Size are rejected client-side before upload (2.7); the server enforces the limit as well. Zip/mixed-selection and extension validation happen client-side before any upload (2.9), and server-side again on receipt.

**Origin guard**: requests carrying an `Origin` header different from the Frontend's origin are rejected with a 4xx User_Error and perform no operation (7.6). Combined with loopback binding this satisfies the privacy requirements without authentication.

**Generation flow** (`POST /api/replays`): pause-independent; the server builds the effective Mapping_Config from persisted settings plus default smoothing/hysteresis (5.2), checks the replay cache, and on a miss runs pipeline processing + sonification generation. While a generation runs, further generate requests are rejected with `GENERATION_IN_PROGRESS` and the running generation continues unchanged (3.10).

### Replay caching (backend/api/replay_cache.py)

Cache key = (Input_Fingerprint, software version, effective Mapping_Config with defaults applied — including Target_Duration and Random_Seed). On a generation request whose key matches a stored Replay record with its WAV + manifest present in the Data_Directory, the Backend returns the stored Replay within 2 s on the Reference_Machine without rendering (5.7). Records with missing files are ignored (treated as a cache miss).

### Settings service (backend/api/settings_service.py)

One application-wide settings record in the Metadata_Store: Target_Duration, per-metric target and sensitivity, Random_Seed, Display_Units (5.3). `PUT` validates every value against the Settings_Panel options/ranges and runs the resulting config through the Mapping_Config_Parser validation; any violation returns a User_Error naming the offending setting and its allowed values and leaves persisted settings unchanged (5.9). Reads return the persisted record, or the defaults (Default_Mapping, 3 min, default seed, imperial) when nothing is persisted (5.10). Persistence completes within 1 s of a valid change and never triggers generation (5.3).

### Frontend

Static, no build step, vanilla ES modules. All assets are served locally; no CDN, external fonts, or external origins anywhere (7.1, 7.2). The non-medical statement ("aesthetic auditory replay … not a medical device, diagnostic tool, or alarm … no diagnosis or health advice") is always visible on the Import_View and Main_Screen at 1280×720 without scrolling (1.5). The Frontend never displays scores, ratings, diagnoses, or medical condition names — only Sleep_Stage names, Soundscape_Preset names, and Event_Vocabulary entries (1.4).

`api.js` wraps `fetch`: parses the User_Error JSON shape, and converts network failures into a "Backend is not reachable — start it with the documented command" message while leaving displayed content unchanged (8.3, 8.4, 2.10, 3.8).

#### Import_View (frontend/js/import_view.js) — Requirement 2

- Two file selectors: Fitbit (multiple `.json`/`.csv` or exactly one `.zip`, case-insensitive) and SensorPush (multiple `.csv`) (2.1, 2.2). Invalid combinations (zip + other files, wrong extension) show a User_Error naming the rejected files and the accepted combinations without uploading (2.9). A file over Max_Upload_Size shows a User_Error naming it and recommending the CLI or selecting only the supported Fitbit files, without uploading (2.7).
- "Use sample data" starts one import of all Sample_Dataset files with default Source_Timezones, no file selection (2.3).
- Per file type from the Input Format Assumptions, an optional Source_Timezone override selectable from IANA identifiers, pre-set to the type's default (UTC or current Display_Timezone), applied only to offset-less timestamps (2.4).
- During import: progress indicator; selectors, overrides, Import, and sample-data actions disabled (2.5). On completion: the Import_Report (accepted files, skipped files with reasons, skipped counts, per-metric counts and coverage in Display_Timezone, warnings) (2.6). On failure: progress hidden, the User_Error (or backend-unreachable message) shown, selections and overrides kept, controls re-enabled (2.10).
- Import sends all selected files of both selectors plus the current overrides in one request (2.8).

#### Main_Screen (frontend/js/main_screen.js) — Requirement 3

- With a session selected: title "Sleep Replay", session label ("Last night" when end_time falls on the current date in Display_Timezone, else "Night of MMM D, YYYY" from the start date), start/end as "h:mm AM/PM → h:mm AM/PM" in Display_Timezone, Generate Replay button, Play button, Playback_Timeline labeled "00:00" … Target_Duration "mm:ss" (3.1–3.3).
- Session selector when >1 candidate: Session_Detector order, start date + time range + duration, selected preselected (3.4); changing the selection stops playback, unloads the Replay, shows the new label/range (3.13).
- Generate pauses playback and requests generation with the current settings (3.5). During generation: progress indicator within 1 s, Generate/Play/selector disabled (3.6). On completion: new Replay loaded at 00:00 with markers and end label from its manifest, controls re-enabled, playback not started (3.7). On failure: User_Error description + action (or unreachable message), Generate and selector re-enabled, previously loaded Replay kept playable (3.8).
- Manifest warnings and unavailable metrics are displayed until the Replay is unloaded or replaced (3.9).
- Content limited to the listed elements (3.11). With no Replay loaded: Play disabled, playhead at 00:00, no markers/state/environmental values, end label = settings Target_Duration (3.12). `NO_SLEEP_SESSION` shows the error plus manual start/end inputs in Display_Timezone and disables Generate until a session is selected (3.14).

#### Playback_Timeline and playback (frontend/js/timeline.js) — Requirement 4

- Playback uses an `HTMLAudioElement` on the WAV URL; Play starts within 500 ms and swaps to Pause; Pause within 200 ms keeps the playhead (4.1, 4.2). The playhead updates ≥10×/s within 100 ms of the audio position (via `timeupdate` + `requestAnimationFrame`) (4.3).
- Displayed per playhead, all derived only from the loaded manifest (4.12), updating within 200 ms of any position change: the Coarse_State label of the containing segment (start inclusive, end exclusive, final segment at Target_Duration; awake→"Awake", light→"Light Sleep", deep→"Deep Sleep", rem→"REM", asleep→"Asleep", restless→"Restless", unknown→"Unknown") (4.4); Night_Time = session start + playhead × Compression_Ratio in Display_Timezone, "h:mm AM/PM" truncated to the minute (4.5); temperature/humidity/pressure of the containing Feature_Window for metrics listed available, omitted when unavailable, placeholder when missing, in the current Display_Units (0.1 °F/°C, 1 percent, 0.01 inHg/0.1 hPa) (4.6).
- One keyboard-focusable marker per Night_Event at Replay_Time ÷ Target_Duration of the width (within 0.5%), focus order = Night_Time order, accessible name = event label (4.7); hover or focus shows the label until both leave (4.8).
- Click/tap seeks to fraction × Target_Duration, preserving play/pause (4.9). End of Replay: stop, playhead at Target_Duration, Play shown (4.10); Play at the end restarts from 00:00 (4.11). Arrow Left/Right/Home/End seek ±5 s / 00:00 / end, clamped, preserving play/pause (4.13). Loading a new Replay stops the old one, resets to 00:00 with Play shown, and replaces all derived display (4.14). Audio load/decode/play failure shows an error with the required action, Play button, playhead kept (4.15).

#### Settings_Panel (frontend/js/settings_panel.js) — Requirement 5

- Keyboard-operable labeled controls: Target_Duration (30 s / 2 / 3 / 5 / 10 min); per metric key a target selector (all Sound_Parameters + none) and a sensitivity control 0.0–1.0 in 0.05 steps; Random_Seed integer [0, 4,294,967,295]; Display_Units imperial/metric (5.1).
- Valid changes persist within 1 s without generating (5.3) and are shown on Frontend start, surviving restarts (5.4); nothing persisted → defaults (5.10).
- Restore-defaults resets per-metric targets/sensitivities to the Default_Mapping, leaving Target_Duration, Random_Seed, Display_Units (5.5).
- Invalid input (empty, sensitivity out of range or not a 0.05 multiple, seed out of range) shows an adjacent message stating allowed values, keeps the last valid value for persistence/generation, and clears when valid again (5.6).
- Display_Units changes update Main_Screen units within 1 s without generating (5.8); `units.js` does °C↔°F and hPa↔inHg conversion/formatting.

#### First start (Requirement 6)

When the Frontend starts and the Metadata_Store has no imported data (reported by `GET /api/sessions` / import state), it shows the Import_View with "Use sample data" (6.1); in both setups the Sample_Dataset ships in the Repository/image, no generator run, copy, or download needed (9.4).

## Data Models

### Settings (persisted in the Metadata_Store)

```python
@dataclass(frozen=True)
class Settings:
    target_duration: int              # {30, 120, 180, 300, 600}
    targets: dict[str, str]           # metric key -> Mapping_Target name
    sensitivities: dict[str, float]   # metric key -> 0.0..1.0 step 0.05
    random_seed: int                  # [0, 2**32 - 1]
    display_units: str                # "imperial" | "metric"

DEFAULT_SETTINGS = Settings(180, DEFAULT_MAPPING targets, DEFAULT_MAPPING sensitivities,
                            DEFAULT_RANDOM_SEED, "imperial")
```

`Settings.to_mapping_config()` builds the effective Mapping_Config by combining these values with the default smoothing windows and Hysteresis_Thresholds (5.2).

### API schemas

- `ImportResponse`: Import_Report (as in the pipeline) + `candidates` (id, start, end, duration_s, stage_data, is_main_sleep) + `selected_id | null`.
- `SessionListResponse`: `candidates`, `selected_id | null`, `error?` (for the no-candidates User_Error that drives the manual-range UI).
- `ManualSessionRequest`: `{start: str, end: str}` (ISO 8601 date-times, offset-less interpreted in Display_Timezone).
- `GenerateResponse`: `{replay_id, cached: bool, manifest: Replay_Manifest}`.
- `ErrorResponse`: `{code, description, action, file_name?, reference_id?}`.
- `SettingsPayload`: mirrors `Settings`; every field validated server-side (5.9).

### Replay record (Metadata_Store, from sleep-replay-sonification)

`{id, wav_path, manifest_path, generation_time, input_fingerprint, version, mapping_config_json}` — the cache key is (input_fingerprint, version, mapping_config_json).

## Correctness Properties

*A property is a characteristic or behavior that should hold true across all valid executions of a system — essentially, a formal statement about what the system should do.*

The app layer is mostly I/O wiring, so property-based testing is lighter here than in the pipeline and sonification specs; Requirement 10 mandates example-based valid/invalid request tests per endpoint rather than properties. Three universal behaviors are worth stating as properties:

### Property 1: Settings persistence round trip

*For any* valid Settings value, persisting it through the settings service and reading it back (including after a simulated Backend restart) returns an equal Settings value, and the effective Mapping_Config built from it is equivalent to the one built before persisting.

**Validates: Requirements 5.3, 5.4**

### Property 2: Replay cache equivalence

*For any* generation request whose (Input_Fingerprint, software version, effective Mapping_Config) matches a stored Replay record with its files present, the Backend returns the stored Replay without invoking the renderer, and the returned manifest is byte-identical to the stored one; any change to the key misses the cache.

**Validates: Requirements 5.7**

### Property 3: Error response structure and status class

*For any* Backend_API endpoint and any failure, the response body is a User_Error-shaped JSON document (code, description, action, optional file_name, optional reference_id) with a 4xx status when the cause is user-provided input or request state and a 5xx status otherwise, and the description contains no traceback, exception type name, or source reference.

**Validates: Requirements 8.1, 8.2**

## Error Handling

The app spec extends the established principle: structured User_Errors with stable codes end to end, internal errors with reference identifiers, and unreachable-Backend messaging in the Frontend.

- **4xx (user input / request state)**: invalid uploads (extension, zip mix, size), invalid timezone overrides, `INVALID_MANUAL_RANGE`, `NO_SLEEP_SESSION`, invalid settings values (5.9), invalid Mapping_Config, `INVALID_TARGET_DURATION`, `INVALID_RANDOM_SEED`, `NO_USABLE_DATA`, `GENERATION_IN_PROGRESS` (3.10), cross-origin requests (7.6).
- **5xx (internal)**: unexpected exceptions during import, processing, rendering, or storage. Logged with a unique reference identifier and metadata only (no telemetry values or per-sample timestamps); the response carries the same reference id and the action to retry and report the id if it recurs (8.2).
- **Frontend display**: every User_Error shows description + required action (8.3). Network failure → "Backend is not reachable" with the documented start command, previously displayed content unchanged (8.4, 2.10, 3.8).
- **Atomicity**: failed generation keeps the previously loaded Replay playable (3.8); failed settings updates leave persisted settings unchanged (5.9); failed imports keep selections and overrides (2.10); write failures during generation clean up partial files (sonification 8.14).
- **Startup errors**: port in use identifies the port and leaves the process unstarted (9.9); Python < 3.11 stops before requests with the detected version (9.8); non-writable Data_Directory exits non-zero (pipeline 15.4).

## Testing Strategy

Requirement 10 defines the suite shape: endpoint tests (valid + invalid per endpoint), settings persistence, and Replay caching, under the same single command (`python -m pytest`) and constraints as the earlier specs.

### Tooling

- `pytest` + Hypothesis for the three properties; FastAPI `TestClient` for API tests (no real sockets needed); the `tmp_data_dir` fixture from the pipeline keeps all writes in temporary directories (10.6).
- Frontend logic is tested at two levels: pure display helpers (session labels, Night_Time math, unit conversion, segment lookup, marker positioning) as importable JS-free reference tests mirrored in Python where they reimplement Backend rules, plus DOM-level smoke tests using a headless browser fixture where available, marked optional so the suite passes without a browser.

### Property-based tests

One test per property, ≥100 examples with faked renderer/storage where rendering is not under test, tagged `# Feature: sleep-replay-app, Property N: ...`:

| Property | Requirement(s) | Min cases |
|---|---|---|
| 1 Settings round trip | 5.3, 5.4 | 100 |
| 2 Replay cache equivalence | 5.7 | 100 |
| 3 Error structure/status class | 8.1, 8.2 | 100 |

### Endpoint tests (Requirement 10.2)

For every endpoint the Frontend calls: at least one valid-request test asserting response content, and for every endpoint taking parameters or a body, at least one invalid-request test asserting the User_Error structure and status class. Coverage includes: import (valid files; bad extension; zip + files; oversized), sample import, session list/select/manual (valid; end ≤ start; >24 h), settings get/put (valid; each invalid field naming the setting), generate (success; while-running rejection; cache hit within 2 s; no-session), manifest/audio fetch (found; unknown id).

### Flow and integration tests

- **End-to-end flow through the API** (1.2): sample import → sessions → generate → manifest/audio fetch → settings change → regenerate with new manifest — asserting each step's contract.
- **Replay caching** (5.7): generate, generate again with identical settings → `cached: true`, no renderer invocation (spy), byte-identical manifest; change one setting → miss.
- **Settings** (5.3–5.10): persistence within 1 s without generation; restart persistence; restore-defaults scope; each validation message; Display_Units conversion values (°C↔°F, hPa↔inHg).
- **Privacy** (7): no external origins referenced by any committed Frontend asset (static scan); loopback binding by default in both setups; cross-Origin request rejected with no data returned (7.6); non-loopback override prints the warning (7.5).
- **Local-first** (1.6, 10.5): the suite runs identically with outbound network disabled (socket-blocking fixture).
- **Setup** (9): `docker compose config` validates and publishes only 127.0.0.1 ports; `.gitignore`/`.dockerignore` rules cover the default Data_Directory but not `sample_data/`/`examples/` (7.4); pinned-dependency scan asserts exact versions and no floating tags (9.5).
- **Documentation sync** (11.14): a test fails when a User_Error code defined in the Backend is missing from `docs/troubleshooting.md`.
- **Storage boundary** (10.6): every test artifact lands in temporary directories; committed files unmodified.

### Suite constraints (Requirement 10.3–10.6)

Same command, inputs, 5-minute budget, network-disabled equivalence, and temp-directory rules as the earlier specs; the app tests reuse the pipeline's fixtures and the Sample_Dataset, and reuse short (30 s) Replays or a faked renderer wherever audio content is not under test.
