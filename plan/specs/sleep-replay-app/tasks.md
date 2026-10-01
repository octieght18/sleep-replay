# Implementation Plan: sleep-replay-app

## Overview

The app layer is built top-down from the Backend_API to the Frontend to setup and documentation. First the API foundation (app factory, error middleware, loopback/origin guards), then the route groups (imports, sessions, settings, replays) over the pipeline and sonification services, with settings persistence and Replay caching as their own components. The static Frontend follows view by view (Import_View, Main_Screen, Playback_Timeline, Settings_Panel), then privacy guards, Docker and non-Docker setup, and the documentation deliverables including the troubleshooting sync test. API tests use FastAPI's TestClient; the three correctness properties from design.md sit next to their components.

## Tasks

- [ ] 1. Implement the Backend_API foundation
  - [ ] 1.1 Set up the API package and app factory
    - Pin `fastapi` and `uvicorn` to exact versions in the requirements; `backend/api/app.py`: app factory wiring the pipeline startup (Data_Directory check, Display_Timezone resolution, adapter registration) and all route modules
    - Default bind 127.0.0.1 port 8735; `SLEEP_REPLAY_API_HOST` override prints the no-authentication startup warning when non-loopback; port-in-use check exits with an error identifying the port; Python < 3.11 exits with the detected version before requests are accepted
    - _Requirements: 1.8, 7.3, 7.5, 9.8, 9.9_
  - [ ] 1.2 Implement error mapping and reference-id logging
    - `backend/api/errors.py`: exception middleware mapping `User_Error` to `{code, description, action, file_name}` JSON with a 4xx status for user-input/request-state causes and 5xx for internal causes; unexpected exceptions logged (metadata only, unique reference id, no telemetry values or per-sample timestamps) and returned as a 5xx User_Error carrying the same reference id and a retry/report action
    - _Requirements: 8.1, 8.2_
  - [ ] 1.3 Implement the origin guard
    - In `app.py`: requests with an `Origin` header different from the Frontend's origin are rejected with a 4xx User_Error, performing no operation and returning no session, telemetry, or Replay data
    - _Requirements: 7.6_
  - [ ] 1.4 Write property test for error structure and status class
    - **Property 3: Error response structure and status class**
    - **Validates: Requirements 8.1, 8.2**
    - Hypothesis, ≥100 examples across endpoints and injected failure kinds, tag `# Feature: sleep-replay-app, Property 3: ...`

- [ ] 2. Implement the import endpoints
  - [ ] 2.1 Implement file import
    - `backend/api/routes_imports.py`: `POST /api/imports` accepting multipart uploads (Fitbit files or one zip plus SensorPush CSVs) with per-file-type Source_Timezone overrides; server-side extension/zip-combination validation and Max_Upload_Size enforcement; uploads streamed to temp files inside the Data_Directory; runs the registry import via `api/pipeline.py`; returns the Import_Report plus the candidate list and selection state
    - _Requirements: 2.4, 2.7, 2.8, 2.9_
  - [ ] 2.2 Implement the sample-data import endpoint
    - `POST /api/imports/sample`: imports the committed Sample_Dataset with Sample_Timezone defaults through `use_sample_data()`; available in both Docker and non-Docker setups with no user file handling
    - _Requirements: 2.3, 6.1, 9.4_
  - [ ] 2.3 Write import endpoint tests
    - Valid import report content; invalid extension; zip plus other files; oversized upload; invalid timezone override (4xx with structure); sample import happy path
    - _Requirements: 2.3, 2.7, 2.9, 8.1, 10.2_

- [ ] 3. Implement the session endpoints
  - [ ] 3.1 Implement listing, selection, and manual ranges
    - `backend/api/routes_sessions.py`: `GET /api/sessions` (candidates in Session_Detector order with Display_Timezone start/end, duration, stage-data availability, selected id; `NO_SLEEP_SESSION` surfaced as structured error state); `PUT /api/sessions/selected` (user choice); `POST /api/sessions/manual` (Display_Timezone range through Session_Detector validation; `INVALID_MANUAL_RANGE` keeps the current selection)
    - _Requirements: 3.4, 3.13, 3.14_
  - [ ] 3.2 Write session endpoint tests
    - Listing content and order; selection replacement; manual range valid/invalid (unparseable, end ≤ start, >24 h) with selection kept; empty-candidates state
    - _Requirements: 3.4, 3.14, 8.1, 10.2_

- [ ] 4. Implement settings persistence
  - [ ] 4.1 Implement the settings service and endpoints
    - `backend/api/settings_service.py`: `Settings` model, `DEFAULT_SETTINGS`, `to_mapping_config()` (current values + default smoothing/hysteresis); one application-wide record in the Metadata_Store, persisted within 1 s of a valid change without generating
    - `backend/api/routes_settings.py`: `GET /api/settings` (persisted values or defaults) and `PUT /api/settings` (per-field validation of Target_Duration, targets, 0.05-step sensitivities, seed range, Display_Units, plus Mapping_Config_Parser validation; invalid values return a User_Error naming the setting and allowed values and leave persisted settings unchanged)
    - _Requirements: 5.1, 5.2, 5.3, 5.9, 5.10_
  - [ ] 4.2 Write property test for settings persistence round trip
    - **Property 1: Settings persistence round trip**
    - **Validates: Requirements 5.3, 5.4**
    - ≥100 examples including a simulated Backend restart
  - [ ] 4.3 Write settings endpoint tests
    - Defaults when nothing persisted; persistence timing and no generation; each invalid field's error content; persisted settings unchanged on rejection
    - _Requirements: 5.3, 5.9, 5.10, 10.1, 10.2_

- [ ] 5. Implement Replay generation and caching
  - [ ] 5.1 Implement the Replay cache
    - `backend/api/replay_cache.py`: cache key (Input_Fingerprint, software version, effective Mapping_Config with defaults including Target_Duration and Random_Seed); hit requires the stored WAV and manifest present in the Data_Directory; hit returns within 2 s without invoking the renderer
    - _Requirements: 5.7_
  - [ ] 5.2 Implement the generate and fetch endpoints
    - `backend/api/routes_replays.py`: `POST /api/replays` (config from current settings; single-flight — a concurrent request gets `GENERATION_IN_PROGRESS` with 4xx and the running generation continues; cache check then pipeline processing + `generate_replay`); `GET /api/replays/{id}/manifest`; `GET /api/replays/{id}/audio` with range support for seeking
    - _Requirements: 3.5, 3.10, 5.2_
  - [ ] 5.3 Write property test for Replay cache equivalence
    - **Property 2: Replay cache equivalence**
    - **Validates: Requirements 5.7**
    - ≥100 examples with a faked renderer: matching keys return the stored Replay byte-identically without rendering; any key change misses
  - [ ] 5.4 Write generation endpoint tests
    - Success contract; concurrent-request rejection; cache hit within 2 s; `NO_USABLE_DATA`/invalid duration as 4xx; unknown replay id; manifest and audio fetch content
    - _Requirements: 3.10, 5.7, 8.1, 10.1, 10.2_

- [ ] 6. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 7. Implement the Frontend foundation
  - [ ] 7.1 Implement the static server and app shell
    - `backend/api/static_server.py`: stdlib static file server bound to 127.0.0.1 port 8734 (same override/warning and port-in-use behavior as the API); `frontend/index.html` + `styles.css`: app shell with the always-visible non-medical statement at 1280×720 and navigation between Import_View, Main_Screen, Settings_Panel
    - `frontend/js/api.js`: fetch wrapper parsing the User_Error shape and converting network failures into the backend-unreachable message with the documented start command, leaving displayed content unchanged
    - _Requirements: 1.5, 1.8, 7.3, 7.5, 8.3, 8.4, 9.9_
  - [ ] 7.2 Write asset and privacy static tests
    - Scan committed Frontend assets: no external origin references (CDN, hosted fonts); loopback defaults in both setups; non-medical statement present in the shell
    - _Requirements: 1.5, 7.1, 7.2_

- [ ] 8. Implement the Import_View
  - [ ] 8.1 Implement selectors, overrides, and import flow
    - `frontend/js/import_view.js`: Fitbit selector (multiple `.json`/`.csv` or one `.zip`, case-insensitive) and SensorPush selector (multiple `.csv`); pre-upload validation (zip+files, wrong extension, over Max_Upload_Size) showing User_Errors naming the files without uploading; per-file-type IANA Source_Timezone overrides pre-set to defaults; one import request for all selected files plus overrides; "Use sample data" action; progress indicator with controls disabled during import; Import_Report display (accepted/skipped files with reasons, skipped counts, per-metric counts and coverage in Display_Timezone, warnings); failure keeps selections/overrides and re-enables controls
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 2.8, 2.9, 2.10_
  - [ ] 8.2 Implement first-start behavior
    - On Frontend start with no imported data in the Metadata_Store, show the Import_View with "Use sample data"
    - _Requirements: 6.1_
  - [ ] 8.3 Write Import_View tests
    - Selector validation cases; override defaults; progress/disable behavior; report rendering; failure recovery state; first-start routing
    - _Requirements: 2.1–2.10, 6.1, 10.1_

- [ ] 9. Implement the Main_Screen
  - [ ] 9.1 Implement session display and generation flow
    - `frontend/js/main_screen.js`: title, session label ("Last night" / "Night of MMM D, YYYY"), "h:mm AM/PM → h:mm AM/PM" range, Generate and Play buttons, timeline end labels; session selector (Session_Detector order, preselected; changing selection stops playback, unloads the Replay, updates label/range); Generate pauses playback and requests generation with current settings; progress within 1 s with controls disabled; completion loads the Replay at 00:00 without starting playback; failure keeps the previous Replay playable and re-enables controls; manifest warnings and unavailable metrics displayed until unload/replace; no-Replay state (Play disabled, no markers/state/values, settings Target_Duration end label); `NO_SLEEP_SESSION` state with manual range inputs and Generate disabled
    - Content restricted to the permitted element list
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8, 3.9, 3.11, 3.12, 3.13, 3.14_
  - [ ] 9.2 Write Main_Screen tests
    - Label rules (current vs other date); selector behavior; generation flow states; failure recovery; warnings/unavailable display; no-session manual-range state
    - _Requirements: 3.1–3.14, 10.1_

- [ ] 10. Implement playback and the Playback_Timeline
  - [ ] 10.1 Implement playback synchronization
    - `frontend/js/timeline.js`: HTMLAudioElement playback (Play ≤500 ms, Pause ≤200 ms, playhead ≥10 Hz within 100 ms of audio position); Coarse_State label of the containing segment with the fixed name mapping; Night_Time = start + playhead × Compression_Ratio in Display_Timezone, minute-truncated 12-hour; per-window environmental values for available metrics in current Display_Units (0.1 °F/°C, 1 percent, 0.01 inHg/0.1 hPa), omitted/placeholder otherwise — all derived only from the loaded manifest, updated within 200 ms of position changes
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 4.12_
  - [ ] 10.2 Implement markers, seeking, and keyboard control
    - One focusable marker per Night_Event at Replay_Time ÷ Target_Duration of width (±0.5%), focus order = Night_Time order, accessible name = label; label shown on hover/focus until both leave; click/tap seek preserving play state; end-of-Replay stop with playhead at Target_Duration; Play at end restarts; Arrow/Home/End seeking (±5 s, 00:00, end, clamped, state preserved); new Replay resets to 00:00 and replaces all derived display; audio load/decode/play failure shows error + action with Play shown and playhead kept
    - _Requirements: 4.7, 4.8, 4.9, 4.10, 4.11, 4.13, 4.14, 4.15_
  - [ ] 10.3 Implement Display_Units conversion
    - `frontend/js/units.js`: °C↔°F and hPa↔inHg conversion and formatting; Main_Screen reflects a Display_Units change within 1 s without generating
    - _Requirements: 5.8_
  - [ ] 10.4 Write playback tests
    - Segment lookup (boundaries, final segment); Night_Time math; unit conversion values and formatting; marker positions and focus order; seek math and clamping; end-of-replay and restart; error state
    - _Requirements: 4.4, 4.5, 4.6, 4.7, 4.9, 4.10, 4.11, 4.13, 5.8, 10.1_

- [ ] 11. Implement the Settings_Panel
  - [ ] 11.1 Implement controls, validation, and persistence
    - `frontend/js/settings_panel.js`: labeled keyboard-operable controls (Target_Duration selector; per-metric target selector + 0.05-step sensitivity; Random_Seed field; Display_Units selector); valid changes persisted within 1 s without generation and displayed on start including after restart; defaults when nothing persisted; restore-defaults resets targets/sensitivities only; invalid input shows an adjacent allowed-values message, keeps the last valid value for persistence/generation, clears on valid
    - _Requirements: 5.1, 5.3, 5.4, 5.5, 5.6, 5.10_
  - [ ] 11.2 Write Settings_Panel tests
    - Control rendering and labels; validation message cases; last-valid-value behavior; restore-defaults scope; persisted values on restart
    - _Requirements: 5.1, 5.4, 5.5, 5.6, 5.10, 10.1_

- [ ] 12. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 13. Implement setup, Docker, and privacy deliverables
  - [ ] 13.1 Implement the Docker setup
    - `docker-compose.yml` + pinned-by-digest Dockerfiles for Backend and Frontend; published ports mapped to 127.0.0.1 only; named volume for the Data_Directory (`SLEEP_REPLAY_DATA_DIR=/data`) persisting across `docker compose down`/`up`; `docker compose up` from a fresh clone builds and starts everything with no other step
    - _Requirements: 9.1, 9.2, 9.5, 9.7_
  - [ ] 13.2 Implement the non-Docker setup
    - Documented Windows PowerShell commands: Python ≥3.11 venv, pinned pip install, Backend and Frontend start scripts; no administrator privileges; no Node.js (no build step); Python version and port checks at start; Backend_API responds within 15 s of the start command on the Reference_Machine
    - _Requirements: 9.3, 9.5, 9.6, 9.8, 9.9_
  - [ ] 13.3 Implement ignore rules and privacy tests
    - `.gitignore`/`.dockerignore` exclude the default Data_Directory while keeping `sample_data/` and `examples/` tracked; tests: compose publishes loopback-only ports, dependency scan finds only exact pins, origin-guard rejection returns no data, non-loopback override prints the warning, network-disabled suite equivalence
    - _Requirements: 1.6, 7.3, 7.4, 7.5, 7.6, 9.5, 10.5_

- [ ] 14. Implement the end-to-end API flow test
  - [ ] 14.1 Write the end-to-end flow test
    - Through the API only: sample import → session list → generate (30 s, faked renderer where audio is not under test) → manifest + audio fetch → settings change → regenerate with a new manifest; asserts each step's response contract
    - _Requirements: 1.2, 10.1_
  - [ ] 14.2 Write the local-first flow test
    - The same flow with outbound network disabled (socket-blocking fixture) after installation, all assets served locally
    - _Requirements: 1.6, 7.1, 10.5_

- [ ] 15. Write the documentation deliverables
  - [ ] 15.1 Write the README
    - Prerequisites with minimum versions; exact `docker compose up` commands; exact non-Docker Windows PowerShell commands; the localhost URL; Sample_Dataset Replay steps through the Frontend and the CLI; the single test command; the not-a-medical-device / no-health-advice statement
    - _Requirements: 1.10, 9.1, 9.2, 9.3, 11.2, 11.10_
  - [ ] 15.2 Write the user and data-format docs
    - Numbered Google Takeout and SensorPush export/import steps (Import_View and CLI), Source_Timezone override, staying within size limits; `docs/data-formats.md` consistent with the confirmed Input Format Assumptions (patterns, fields/keywords, units, inference, timestamp formats, default Source_Timezones, Stage_Name_Mapping, unsupported/ignored inputs)
    - _Requirements: 11.3, 11.4_
  - [ ] 15.3 Write the architecture and adapter docs
    - `docs/architecture.md`: components, import→WAV data flow, domain model, Data_Source_Adapter extension steps, the SensorPush cloud API adapter as an unimplemented extension point with credentials outside version control, and the Non-Goal future sources/targets
    - _Requirements: 11.5_
  - [ ] 15.4 Write the mapping documentation and example config
    - `mapping-config.example.yaml` (parser-accepted; Default_Mapping targets/sensitivities; 3 min; default seed; every default smoothing window and Hysteresis_Threshold; per-key comments on allowed targets, sensitivity range, and audible effect) and `docs/mapping-config.md` (per-key default target/sensitivity/direction/Neutral_Value behavior, contribution combination, per-Sound_Parameter layer/audible property, preset per Coarse_State, final default parameter values equal to the Backend's)
    - _Requirements: 11.6, 11.7_
  - [ ] 15.5 Write the data-location, security, CLI, and troubleshooting docs
    - Default Data_Directory paths (Docker volume and non-Docker Windows), the `SLEEP_REPLAY_DATA_DIR` override, the layout within it, and the delete-all-data commands; the no-authentication / loopback / exposure statement; `docs/cli.md` (every command with arguments, options, defaults, exit codes, examples); `docs/troubleshooting.md` (every User_Error code with cause and action, plus every warning condition)
    - _Requirements: 11.8, 11.9, 11.11, 11.13_
  - [ ] 15.6 Add the license and the troubleshooting sync test
    - OSI-approved `LICENSE` at the root; a test that fails when a Backend User_Error code is missing from `docs/troubleshooting.md`
    - _Requirements: 11.12, 11.14_

- [ ] 16. Final checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise; confirm the full suite finishes within 5 minutes on the Reference_Machine.

## Notes

- Each property test uses Hypothesis with `@settings(max_examples=100)`, keeps strategies local to its own file, and carries the tag `# Feature: sleep-replay-app, Property N: <text>`. Properties 2 and 3 use faked renderer/storage so they stay cheap; audio content is covered by the sonification suite.
- Frontend DOM tests that need a headless browser are marked optional so `python -m pytest` passes on a minimal Python-only install; the pure display logic (labels, Night_Time, units, segment lookup, marker math) is always tested.
- The performance-sensitive assertions (cache hit within 2 s, API response within 15 s of start) are Reference_Machine-timed and marked for deselection elsewhere.
- Tasks 13.1/13.2 require Docker/manual verification on Windows; the automated tests assert the static contract (compose file, pins, ignore rules, start scripts) rather than running Docker.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1"] },
    { "id": 1, "tasks": ["1.2", "1.3"] },
    { "id": 2, "tasks": ["1.4", "2.1", "2.2", "3.1", "4.1", "5.1"] },
    { "id": 3, "tasks": ["2.3", "3.2", "4.2", "4.3", "5.2"] },
    { "id": 4, "tasks": ["5.3", "5.4", "7.1"] },
    { "id": 5, "tasks": ["7.2", "8.1", "9.1", "10.3", "11.1"] },
    { "id": 6, "tasks": ["8.2", "9.2", "10.1", "11.2"] },
    { "id": 7, "tasks": ["8.3", "10.2"] },
    { "id": 8, "tasks": ["10.4", "13.3"] },
    { "id": 9, "tasks": ["13.1", "13.2", "14.1"] },
    { "id": 10, "tasks": ["14.2", "15.1", "15.2", "15.3", "15.4", "15.5"] },
    { "id": 11, "tasks": ["15.6"] }
  ]
}
```
