# Implementation Plan: sleep-replay-data-pipeline

## Overview

The pipeline is built in Python (3.11+) bottom-up along the design's one-directional data flow. The order is: domain types and errors, then persistence and timezone foundations, then the adapter registry, the Fitbit and SensorPush importers, the Session_Detector, the Aligner, the compression mapping, the Feature_Extractor, and the Event_Detector. After that come the Sample_Data_Generator and Sample_Dataset. The last step wires everything into an `api/pipeline.py` service layer, with no HTTP (the Backend_API itself is built in sleep-replay-app). Testing uses pytest and Hypothesis. Each correctness property from design.md gets its own property-test sub-task placed next to the code it checks.

## Tasks

- [x] 1. Set up project structure and test tooling
  - [x] 1.1 Create the Backend package skeleton and test configuration
    - Create `backend/` with packages `domain`, `ingestion` (sub-packages `fitbit`, `sensorpush`), `processing`, `persistence`, and placeholder packages `sonification`, `audio`, `api` (each with `__init__.py`), plus `tools/`, `sample_data/`, `tests/`
    - Add `pyproject.toml` / `requirements.txt` with exact pinned versions (Python ≥ 3.11; `pytest`, `hypothesis`, `numpy`, and `tzdata` so `zoneinfo` works on Windows)
    - Configure pytest so `python -m pytest` is the single command that runs the whole suite and exits non-zero on failure
    - Add `tests/conftest.py` with a `tmp_data_dir` fixture that sets `SLEEP_REPLAY_DATA_DIR` to a temporary directory for every test
    - _Requirements: 17.1, 18.3, 18.7_

  - [x] 1.2 Implement the Dependency_Rules test
    - Create `tests/test_dependency_rules.py`: walk every `.py` file under `backend/`, parse it with `ast` (module-level and in-function `import` / `from … import`, absolute and relative), and resolve each import to a Backend package or Importer sub-package
    - Assert rules (a)–(e) from Requirement 17.2. Also assert that `persistence` imports only `domain`
    - On violation, fail with a message naming the violating source file and the imported package
    - _Requirements: 17.2, 17.3_

- [x] 2. Implement the domain model
  - [x] 2.1 Implement telemetry types and unit conversion
    - `backend/domain/telemetry.py`: `Metric` enum, `CONTINUOUS_METRICS`, `CANONICAL_UNIT`, frozen `TelemetryPoint`, and `is_valid(point)` (timezone-aware timestamp, non-empty source, Metric, finite value, allowed unit)
    - `backend/domain/units.py`: the allowed source units per metric (bpm; ms; steps/min; °C/°F; percent; hPa/mbar/inHg/kPa), `to_canonical(metric, value, unit)` (°F→°C; inHg/kPa/mbar→hPa), and an inconvertible-unit error
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.8, 7.1, 10.3_

  - [x] 2.2 Implement stage and session types
    - `backend/domain/stages.py`: `Sleep_Stage`, `STAGE_NAME_MAPPING` with `map_stage_name()` (trimmed, case-insensitive, unknown fallback), `DOMINANT_STAGE_ORDER`, frozen `Stage_Segment` with `is_brief_awakening`
    - `backend/domain/session.py`: frozen `SleepSession` with `log_id`, `is_main_sleep`, `session_hrv`, `source_file`; derived `sleep_onset_time`, `final_wake_time` (None when there's no non-awake/unknown segment), `has_stage_data`, `stage_data_availability` (`stages` / `classic` / `none`), and `stage_at(instant)` returning unknown for uncovered intervals
    - _Requirements: 2.1, 2.3, 2.4, 2.5, 2.6, 2.10, 3.5_

  - [x] 2.3 Implement timeline, feature, and event types
    - `backend/domain/timeline.py`: frozen `Aligned_Timeline` (resolution, timestamps, per-metric values, Missing_Data_Mask, per-sample stages), Timeline_Resolution candidates, Max_Interpolation_Gap defaults
    - `backend/domain/features.py`: `Metric_Window_Features`, `Feature_Window`, `Feature_Series`, `Coarse_State`, Feature_Window_Span, Minimum_State_Duration, smoothing-window defaults
    - `backend/domain/events.py`: `Event_Type`, `EVENT_VOCABULARY_ORDER`, frozen `Night_Event`, and the event threshold and duration defaults (Wake/Major_Transition/Restless durations, Movement thresholds, environmental thresholds and spans, merge window, Max_Event_Count)
    - _Requirements: 10.6, 12.1, 12.2, 13.11, 13.12_

  - [x] 2.4 Implement structured errors and reports
    - `backend/domain/errors.py`: `User_Error` (stable `code`, plain-language `description`, `action`, optional `file_name`), a fixed error-code table (`NO_FITBIT_FILES`, `MULTIPLE_ARCHIVES`, `ARCHIVE_UNREADABLE`, `NO_TIMESTAMP_COLUMN`, `NO_READABLE_ROWS`, `INVALID_TIMEZONE`, `SESSION_TOO_SHORT`, `INVALID_TARGET_DURATION`, `NO_SLEEP_SESSION`, `INVALID_MANUAL_RANGE`, `SAVE_FAILED`, `PERSISTED_DATA_UNREADABLE`, `DATA_DIR_UNWRITABLE`, `INSUFFICIENT_DATA`, `SOURCE_LOAD_FAILED`), `Warning_Item`, `Import_Report` (no telemetry values), `Processing_Report`, and helpers for merging reports
    - _Requirements: 16.1, 16.4_

  - [x] 2.5 Implement the Data_Source_Adapter interface
    - `backend/domain/adapter.py`: the `Source` type (list of local paths, or one `.zip` path), `LoadResult` (telemetry, sessions, session_hrv rows, Import_Report), and a `Data_Source_Adapter` `Protocol` with `source_identifier`, `load(source, tz_context)`, and optional `candidate_sessions`. Docstrings describe the inputs and outputs of both operations, the TelemetryPoint fields, the MVP Metrics and Canonical_Units, and the registration steps
    - _Requirements: 7.1, 7.2, 7.8, 17.4_

  - [x] 2.6 Implement Stage_Segment normalization
    - Add `build_stage_segments(intervals, session_start, session_end)` to `backend/domain/stages.py`. It clips intervals to the session and discards zero-length results. Where exactly one of two overlapping intervals is a Brief_Awakening, it resolves the overlap in that interval's favor; otherwise the later-starting interval wins (for equal starts, the later source order). It splits overlapped intervals so both sides keep their original stage, and returns ordered, non-overlapping segments
    - _Requirements: 2.2, 2.8, 2.9, 3.3, 3.4_

  - [x] 2.7 Write unit tests for domain model
    - Stage_Name_Mapping (case and whitespace, unknown fallback), sleep onset / final wake (defined and undefined), unit conversion values, TelemetryPoint validity, overlap-resolution examples
    - _Requirements: 1.4, 2.3, 2.5, 2.6, 2.8, 2.10, 10.3_

- [x] 3. Implement the persistence foundation
  - [x] 3.1 Implement Data_Directory resolution and startup checks
    - `backend/persistence/data_directory.py`: resolve from non-empty `SLEEP_REPLAY_DATA_DIR`, else a documented default path. Create it, verify it's writable with a probe file, and on failure raise `DATA_DIR_UNWRITABLE` naming the path and the env override, with a helper that exits non-zero before any input is read
    - _Requirements: 15.1, 15.2, 15.4_

  - [x] 3.2 Implement atomic telemetry persistence
    - `backend/persistence/telemetry_store.py`: serialize an import's TelemetryPoints (ISO instant with offset, source, metric, unit, value written so it round-trips exactly), write to a temp file inside the Data_Directory, fsync, then atomically rename. On failure, remove the temp file and raise `SAVE_FAILED`, leaving earlier imports untouched
    - The load function validates every point and raises `PERSISTED_DATA_UNREADABLE` naming the import's source files when the file can't be read or contains an invalid point, returning none of its points
    - _Requirements: 1.7, 1.8, 1.9, 1.10, 16.5_

  - [x] 3.3 Write property test for telemetry persistence round trip
    - **Property 1: TelemetryPoint persistence round trip**
    - **Validates: Requirements 1.8**
    - Hypothesis, ≥100 examples, including the empty list and varied UTC offsets. Tag: `# Feature: sleep-replay-data-pipeline, Property 1: ...`

  - [x] 3.4 Implement the SQLite Metadata_Store
    - `backend/persistence/metadata_store.py`: a database file inside the Data_Directory with tables for imports (id, source identifier, source file names, per-file metadata including the Fitbit file-name date, telemetry file reference, Import_Report JSON), sessions (candidate and selected, selection kind auto/user/manual), settings, and Replay records. All writes run in transactions so a failed operation leaves prior rows unchanged
    - _Requirements: 15.1, 16.5_

  - [x] 3.5 Implement the metadata-only logging helper
    - `backend/persistence/safe_logging.py`: a logger wrapper that accepts only operational metadata fields (file names, counts, durations, error codes, reference ids, descriptions). It writes log files only inside the Data_Directory and strips exception traces of local variable values
    - _Requirements: 15.1, 15.3_

  - [x] 3.6 Write unit tests for persistence
    - Env override honored, non-writable directory gives a non-zero exit with `DATA_DIR_UNWRITABLE`, a simulated write failure keeps earlier imports and leaves no partial file, a corrupted persisted file gives `PERSISTED_DATA_UNREADABLE`, Metadata_Store rollback, logs contain no telemetry values or timestamps
    - _Requirements: 1.9, 1.10, 15.2, 15.3, 15.4, 16.5_

- [x] 4. Implement timezone handling
  - [x] 4.1 Implement timezone localization helpers in the domain package
    - `backend/domain/timezones.py` (stdlib `zoneinfo` only, so the Importers can use it without breaking Dependency_Rule (b)): `validate_iana(name)` raising `INVALID_TIMEZONE`; `resolve_source_timezone(file_type, overrides, display_tz)` using the Input Format Assumptions defaults (Fitbit heart_rate/steps UTC; everything else Display_Timezone); and `localize(naive, tz)` that uses an explicit offset when present, resolves an ambiguous time to the earlier instant, shifts a nonexistent time forward by the gap, and reports whether it adjusted the value. Also a per-file DST adjustment counter that produces one warning per file
    - _Requirements: 9.1, 9.2, 9.3, 9.4, 9.10_

  - [x] 4.2 Implement Display_Timezone resolution and UTC conversion
    - `backend/processing/timezones.py`: read the Display_Timezone from a documented configuration setting at startup (for example `SLEEP_REPLAY_DISPLAY_TIMEZONE`), else the host timezone, else UTC with a Warning_Item stating the reason and how to configure it. Also `to_utc()` and UTC elapsed-seconds helpers
    - _Requirements: 9.5, 9.6, 9.8, 9.11_

  - [x] 4.3 Write unit tests for timezone handling
    - Ambiguous → earlier instant and nonexistent → shift forward, with warning counts; 22:00→06:00 lasts 9 h across fall-back and 7 h across spring-forward; explicit offset beats override; invalid override gives `INVALID_TIMEZONE`; invalid Display_Timezone falls back to UTC with a warning
    - _Requirements: 9.3, 9.4, 9.8, 9.10, 9.11_

- [x] 5. Implement the adapter registry and import orchestration
  - [x] 5.1 Implement the adapter registry with validation gate and atomic import
    - `backend/ingestion/registry.py`: register/look up adapters by unique Source_Identifier. `run_import(source_id, source, tz_overrides, display_tz, stores)` validates overrides first, calls `load` and (if implemented) `candidate_sessions` (missing means zero candidates), and drops invalid TelemetryPoints (naive timestamp, non-MVP metric, non-finite value, inconvertible unit), counting each reason in the Import_Report. It persists the telemetry atomically and records the import in the Metadata_Store before returning the report
    - A whole-source failure returns a User_Error (`SOURCE_LOAD_FAILED` or the adapter's specific code) naming the file and stores nothing
    - _Requirements: 1.7, 7.1, 7.2, 7.4, 7.6, 7.7, 9.10, 16.5_

  - [x] 5.2 Write property test for the validation gate
    - **Property 2: Invalid returned TelemetryPoints are excluded, valid ones retained**
    - **Validates: Requirements 7.7**
    - Uses a fake adapter returning generated mixes of valid and invalid points, ≥100 examples

- [x] 6. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 7. Implement the Fitbit_Importer
  - [x] 7.1 Implement Fitbit timestamp parsing
    - `backend/ingestion/fitbit/timestamps.py`: `MM/DD/YY HH:MM:SS` (years 2000–2099) and ISO 8601 with or without offset, localized through `domain.timezones.localize` with DST adjustment counting
    - _Requirements: 4.1, 4.2, 4.4, 9.1, 9.3, 9.4_

  - [x] 7.2 Implement Fitbit packaging and file matching
    - `backend/ingestion/fitbit/packaging.py`: accept individual files or exactly one `.zip` (case-insensitive). Reject several zips, or a zip mixed with files (`MULTIPLE_ARCHIVES`). Match the five patterns against the final path component, case-insensitively, with 4/2/2-digit dates, at any depth, and extract the file-name date
    - Stream zip members in memory (never write to paths derived from member names). Enforce Max_Archive_Member_Size (200 MB) on the declared and the actual decompressed size. List unmatched files with a total count. Record skip reasons for password-protected or undecompressable members. Unopenable archive gives `ARCHIVE_UNREADABLE`; no match gives `NO_FITBIT_FILES` listing the patterns and Google Takeout steps
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 5.7, 5.9, 5.10, 15.1_

  - [x] 7.3 Implement Fitbit sleep-log parsing
    - `backend/ingestion/fitbit/sleep.py`: one candidate SleepSession per array entry (sleep Source_Timezone). Build segments from `levels.data` and `levels.shortData` (Brief_Awakening) using `build_stage_segments`. Map level names, with one warning per distinct unknown name per file giving the count. Empty, absent, or all-skipped levels produce a single unknown segment plus a warning
    - Skip invalid levels entries (skipped-value count). Skip entries with missing or unparseable startTime/endTime (warning with logId) or end ≤ start (warning naming the file). A non-boolean or missing isMainSleep is recorded as false. Attach logId and source file. Handle JSON parse errors (line number) and non-array top-level values as file skips
    - _Requirements: 2.7, 2.9, 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8, 3.9, 3.10, 3.11, 5.7, 5.8_

  - [x] 7.4 Implement Fitbit heart rate and steps parsing
    - `backend/ingestion/fitbit/intraday.py`: heart_rate (bpm, `value.bpm`) and steps (steps/min, string or numeric `value`) TelemetryPoints. Apply range gates (heart rate 20–250, steps 0–300) to the skipped-value count; empty, non-numeric, or non-finite values to the skipped-value count; unparseable dateTime or missing required fields to the skipped-row count
    - _Requirements: 1.5, 4.1, 4.4, 4.7, 4.8, 5.8_

  - [x] 7.5 Implement Fitbit HRV parsing
    - `backend/ingestion/fitbit/hrv.py`: hrv_rmssd TelemetryPoints (ms) from HRV Details (`timestamp`, `rmssd`, other columns ignored). Parse daily summary rows into dated rmssd values. Add `session_hrv_for(session, rows, display_tz)` returning the mean of rows dated on the session end_time's calendar date. Range gate 0 < rmssd ≤ 500. A missing `timestamp` or `rmssd` column skips the file with that reason
    - _Requirements: 1.5, 4.2, 4.3, 4.7, 4.8, 5.7_

  - [x] 7.6 Implement the Fitbit_Importer adapter
    - `backend/ingestion/fitbit/importer.py`: a `Data_Source_Adapter` with Source_Identifier `fitbit` that implements `load` and `candidate_sessions` by combining 7.2–7.5. It records applied Source_Timezones per file type and per-file file-name dates, and adds `files_in_selection_window(file_dates, start, end, display_tz)` (start date −1 day through end date +1 day). It warns once per metric (heart_rate, hrv_rmssd, steps) missing for a given session. Register it in the registry
    - _Requirements: 4.5, 4.6, 7.3, 9.1, 16.2_

  - [x] 7.7 Write property test for Stage_Segment ordering and containment
    - **Property 3: Stage_Segment ordering and containment invariant**
    - **Validates: Requirements 2.2, 2.9, 3.2**
    - Generates Fitbit sleep-log JSON (overlapping, out-of-bounds, and zero-length levels plus shortData), parses it, and checks the invariant, ≥100 examples

  - [x] 7.8 Write unit tests for Fitbit packaging
    - Individual files and one zip; names differing only in case; nested directories; `..` and absolute member names; oversized member; multiple zips or zip+files; corrupted zip; unrecognized file listed as unsupported; no matching files
    - _Requirements: 5.1–5.10, 18.5_

  - [x] 7.9 Write unit tests for Fitbit sleep, intraday, and HRV parsing
    - Stages-style and classic-style logs with shortData precedence and splitting; unknown level names; missing levels; malformed JSON; missing required fields; range gates; string step values; Session_HRV mean; file-date selection window
    - _Requirements: 3.1–3.11, 4.1–4.8, 18.5_

- [x] 8. Implement the SensorPush_Importer
  - [x] 8.1 Implement SensorPush timestamp parsing
    - `backend/ingestion/sensorpush/timestamps.py`: ISO 8601 (with or without seconds, `T` or space, `±HH:MM`/`Z`) and US `M/D/YYYY h:mm[:ss] AM/PM` and `M/D/YYYY HH:MM[:SS]` (month/day/year). An explicit offset wins, otherwise the Source_Timezone applies via `domain.timezones.localize`
    - _Requirements: 6.6, 9.3, 9.4_

  - [x] 8.2 Implement CSV reading and header matching
    - `backend/ingestion/sensorpush/csv_reader.py`: detect comma or semicolon delimiters, strip a UTF-8 BOM, ignore blank lines. Match timestamp, temperature (not `dew`), humidity/`rh` (not `absolute`), pressure/`baro`, and sensor id columns by case-insensitive keyword; the leftmost column wins on duplicates, with a warning naming the ignored headers. Other columns are ignored silently
    - _Requirements: 6.2, 6.5, 6.12_

  - [x] 8.3 Implement unit token recognition and inference
    - `backend/ingestion/sensorpush/units.py`: header unit tokens, with standalone-only `F`/`C` matching. Median-based inference when no unit is given (temperature >45 → °F; pressure 25–32 inHg, 90–110 kPa, 900–1100 hPa; humidity always percent) with a warning. An undeterminable pressure unit drops the pressure column with a warning
    - _Requirements: 6.3, 6.4, 6.14_

  - [x] 8.4 Implement the SensorPush_Importer adapter
    - `backend/ingestion/sensorpush/importer.py`: a `Data_Source_Adapter` with Source_Identifier `sensorpush` implementing `load` only. It creates one TelemetryPoint per parseable cell and appends the sensor id to the source (plain `sensorpush` when the id is empty). Bad timestamp: skip the row (skipped-row +1). Empty or non-numeric cell: skip the value (skipped-value +1)
    - A missing timestamp or all-measurement-columns-missing file gives `NO_TIMESTAMP_COLUMN` listing the detected headers and expected keywords. No data rows or no points gives `NO_READABLE_ROWS` listing the formats. A partially missing metric gives one warning per metric. Register it in the registry
    - _Requirements: 1.5, 6.1, 6.7, 6.8, 6.9, 6.10, 6.11, 6.13, 7.3_

  - [x] 8.5 Write unit tests for SensorPush import
    - Comma and semicolon files, each with and without a BOM; every header keyword and unit token; unitless headers (inference); every timestamp format; sensor id handling; duplicate columns; malformed CSV; no timestamp column; no readable rows
    - _Requirements: 6.1–6.14, 18.5_

- [x] 9. Implement the Session_Detector
  - [x] 9.1 Implement candidate deduplication and merging
    - `backend/processing/session_detector.py`: dedup on shared logId or identical start/end (prefer main sleep, then `stages`, then the first-sorting file name) with a discarded count. Merge strict overlaps to the union interval, with the overlap taking segments from the main-sleep candidate (else the longer one, else the earlier-starting one), keeping outside segments and OR-ing the main-sleep flag. Repeat to a fixpoint and warn with the merged bounds
    - _Requirements: 8.5, 8.6_

  - [x] 9.2 Write property test for dedup and merge fixpoint
    - **Property 4: Session deduplication and merge fixpoint**
    - **Validates: Requirements 8.5, 8.6**
    - ≥100 examples of generated candidate sets with shared logIds, identical bounds, touching and overlapping intervals

  - [x] 9.3 Implement listing and selection
    - In `session_detector.py`: list candidates latest start → earliest with Display_Timezone start/end, h:m duration, and stage-data availability. Automatic selection picks the latest main-sleep candidate, else the longest candidate on the latest end date (the later start breaks ties), only while no user or manual selection exists
    - User choice replaces the selection. A manual range is parsed in Display_Timezone and becomes one unknown segment with no main-sleep flag; missing, unparseable, end ≤ start, or >24 h ranges give `INVALID_MANUAL_RANGE` and keep the selection. No candidates and no manual selection gives `NO_SLEEP_SESSION` offering manual definition. A user or manual selection persists across imports, following the merged session that contains it
    - _Requirements: 8.1, 8.2, 8.3, 8.4, 8.7, 8.8, 8.9, 8.11_

  - [x] 9.4 Implement telemetry attachment
    - In `session_detector.py`: `attach_telemetry(session, points)` returns the session with exactly the points where start_time ≤ instant ≤ end_time
    - _Requirements: 2.1, 8.10_

  - [x] 9.5 Write property test for the telemetry attachment boundary
    - **Property 5: Telemetry attachment boundary**
    - **Validates: Requirements 8.10, 2.1**
    - ≥100 examples, including points exactly at the boundaries and in mixed offsets

  - [x] 9.6 Write unit tests for session selection
    - Main-sleep latest; longest on the latest date with ties; user choice replacing a manual selection; manual range validation; empty-candidates error; selection kept across imports and after a merge
    - _Requirements: 8.1–8.4, 8.7–8.9, 8.11_

- [x] 10. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 11. Implement the Aligner and resampling
  - [x] 11.1 Implement resampling primitives
    - `backend/processing/resample.py`: Timeline_Resolution selection (smallest candidate ≥ shortest median interval among Continuous_Metrics with ≥2 points; 60 s fallback); the sample grid start + k·res < end in UTC elapsed time; the per-Sample_Interval mean; linear interpolation at the sample timestamp when the bracketing values are ≤ Max_Interpolation_Gap apart, else missing with a gap entry (metric, last-value timestamp, elapsed); no extrapolation
    - Steps: hold unchanged over [ts, ts+60 s), missing elsewhere. Stage per sample from the half-open segment containing it, else unknown
    - _Requirements: 9.8, 10.4, 10.5, 10.6, 10.7, 10.8, 10.9, 10.10, 10.11, 10.12, 10.15, 10.17_

  - [x] 11.2 Implement the Aligner and Input_Fingerprint
    - `backend/processing/aligner.py`: `align(session, nearby_heart_rate=()) -> (Aligned_Timeline, Processing_Report)`. Steps: exclude inconvertible units with a warning (source, metric, unit, count); convert to Canonical_Unit and UTC; sort; collapse same (source, metric, timestamp) duplicates to the mean with per-source/metric counts; pick one environmental sensor (most points, lexicographic tie) with a warning; resample
    - Report unavailable metrics and one gap warning per metric with the gap count and total duration. Add the heart_rate Source_Timezone mismatch warning when coverage is under 50% and nearby points exist within 14 h. Every step is independent of input order
    - `backend/processing/fingerprint.py`: SHA-256 over canonically sorted, UTC-normalized TelemetryPoints and Stage_Segments
    - _Requirements: 9.5, 9.9, 10.1, 10.2, 10.3, 10.13, 10.14, 10.16, 10.18, 16.4_

  - [x] 11.3 Write property test for Display_Timezone independence
    - **Property 6: Alignment is independent of the Display_Timezone**
    - **Validates: Requirements 9.5**

  - [x] 11.4 Write property test for aligned values within measured range
    - **Property 7: Aligned values stay within the measured range**
    - **Validates: Requirements 10.14**

  - [x] 11.5 Write property test for sample count
    - **Property 8: Aligned_Timeline sample count**
    - **Validates: Requirements 10.15**

  - [x] 11.6 Write property test for alignment confluence
    - **Property 9: Alignment is confluent (order-independent)**
    - **Validates: Requirements 10.18**

  - [x] 11.7 Write property test for interpolation-gap boundary behavior
    - **Property 10: Interpolation-gap boundary behavior**
    - **Validates: Requirements 10.8, 10.9, 10.10**

  - [x] 11.8 Write unit tests for the Aligner
    - Resolution choice and 60 s fallback; interpolation exactly at and just over each Max_Interpolation_Gap; steps hold; stage assignment at boundaries; duplicate collapse; sensor selection; inconvertible unit; gap and timezone-mismatch warnings; a session spanning a DST transition
    - _Requirements: 9.8, 9.9, 10.2, 10.4, 10.5, 10.8, 10.9, 10.11, 10.12, 10.13, 10.16, 18.1_

- [x] 12. Implement night compression mapping
  - [x] 12.1 Implement Target_Duration handling and time mapping
    - `backend/processing/compression.py`: the allowed durations {30, 120, 180, 300, 600}; `select_target_duration(request, config)` (request → config → 180) raising `INVALID_TARGET_DURATION` listing the accepted values; `compression_ratio(session, target)` using UTC elapsed seconds, with a `SESSION_TOO_SHORT` error listing the shorter offered durations or saying the session is too short; `replay_time(t)` with exact endpoints and strict monotonicity
    - _Requirements: 11.1, 11.2, 11.3, 11.4, 11.5, 11.6, 11.7_

  - [x] 12.2 Write property test for mapping accuracy
    - **Property 11: Night_Time to Replay_Time mapping accuracy**
    - **Validates: Requirements 11.4**

  - [x] 12.3 Write property test for mapping order preservation
    - **Property 12: Night_Time to Replay_Time mapping is strictly order-preserving**
    - **Validates: Requirements 11.5**

  - [x] 12.4 Write unit tests for Target_Duration validation
    - Precedence; disallowed values; session ≤ target; 8 h / 3 min gives 160.000
    - _Requirements: 11.2, 11.3, 11.6, 11.7_

- [x] 13. Implement the Feature_Extractor
  - [x] 13.1 Implement Feature_Windows and per-metric statistics
    - `backend/processing/features.py`: ceil(Target_Duration / 0.5 s) consecutive windows of Compression_Ratio × 0.5 s. The last window ends at end_time and includes the end sample. Compute mean, min, and max over non-missing samples; least-squares slope per hour; missing fraction (1.0 and missing stats when the window is empty); and the smoothed value over the metric's smoothing window centered on the midpoint and truncated to the session
    - _Requirements: 12.1, 12.2, 12.3, 12.4, 12.5, 12.10_

  - [x] 13.2 Implement stage fractions and Movement_Intensity
    - In `features.py`: per-window stage fractions from Stage_Segments (Brief_Awakening precedence, uncovered time counted as unknown, sum 1.0) and the dominant stage using `DOMINANT_STAGE_ORDER`. Movement_Intensity = `min(1.0, mean_steps / MOVEMENT_FULL_SCALE)` with a documented constant, 0.0 when a window has no steps. When the session has no steps at all, fall back to the restless/Brief_Awakening coverage fraction with a Processing_Report warning
    - _Requirements: 12.6, 12.7, 12.8, 12.11_

  - [x] 13.3 Implement Coarse_State smoothing
    - In `features.py`: group runs by dominant stage and merge runs shorter than Minimum_State_Duration (1.0 s = 2 windows) into the preceding run (the following run for the first), recombine equal neighbors, and repeat to a fixpoint. Expose `extract(timeline, session, target_duration) -> (Feature_Series, list[Coarse_State], Processing_Report)`
    - _Requirements: 12.9, 12.12_

  - [x] 13.4 Write property test for Movement_Intensity monotonicity and bounds
    - **Property 13: Movement_Intensity is monotone in mean steps and bounded**
    - **Validates: Requirements 12.7**

  - [x] 13.5 Write property test for Feature_Window coverage and count
    - **Property 14: Feature_Windows cover the session with the required count**
    - **Validates: Requirements 12.10**

  - [x] 13.6 Write property test for per-window numeric invariants
    - **Property 15: Per-window numeric invariants**
    - **Validates: Requirements 12.11**

  - [x] 13.7 Write property test for Coarse_State length and minimum run
    - **Property 16: Coarse_State length and minimum-run invariant**
    - **Validates: Requirements 12.12**

  - [x] 13.8 Write unit tests for feature extraction
    - Slope units, smoothing truncation, dominant-stage tie order, Brief_Awakening precedence, stage-derived movement fallback warning, a Coarse_State short-first-run example
    - _Requirements: 12.2, 12.4, 12.6, 12.8, 12.9, 18.1_

- [x] 14. Implement the Event_Detector
  - [x] 14.1 Implement stage-based events
    - `backend/processing/events.py`: Stage_Runs from the Aligned_Timeline. Sleep onset and awakening (magnitude 1.0) when defined; awake runs after onset, ending ≤ final wake, ≥5 min; light/deep/rem runs after onset ≥10 min; magnitudes per 13.14. No events when there is no non-awake/unknown stage
    - _Requirements: 13.1, 13.2, 13.3, 13.4, 13.14, 13.15_

  - [x] 14.2 Implement movement and restless events
    - In `events.py`: Movement_Runs over Feature_Windows. A movement event at the start of each run ≥0.6 (magnitude = max); a restless period at the start of each run ≥0.3 lasting ≥10 min (magnitude = mean)
    - _Requirements: 13.5, 13.6, 13.14_

  - [x] 14.3 Implement environmental change events
    - In `events.py`: for temperature, humidity, and pressure, walk the samples comparing the smoothed value to earlier non-missing samples within the change span (30 min / 30 min / 3 h), restricted to pairs at or after the last emitted event of that metric. Emit rising or falling from the sign of the greatest-absolute difference when it's ≥ the threshold; magnitude min(1, |Δ|/(2·threshold))
    - _Requirements: 13.7, 13.8, 13.14_

  - [x] 14.4 Implement merge, cap, ordering, and labeling
    - In `events.py`: `detect(session, timeline, features, coarse_states, target_duration, display_tz) -> list[Night_Event]`. Merge same-type events within 15 min (keep the earlier, max magnitude). Cap at 12, keeping onset and awakening and then by descending magnitude with the earlier time breaking ties. Compute Replay_Time via `compression.replay_time`, clamp magnitude to [0, 1], label `HH:MM <description>` in Display_Timezone, sort by Night_Time then vocabulary order. An empty result is valid
    - _Requirements: 13.9, 13.10, 13.11, 13.12, 13.13, 13.16_

  - [x] 14.5 Write property test for Night_Event Replay_Time bounds
    - **Property 17: Night_Event Replay_Time bounds**
    - **Validates: Requirements 13.13**

  - [x] 14.6 Write property test for Night_Event magnitude bounds
    - **Property 18: Night_Event magnitude bounds**
    - **Validates: Requirements 13.14**

  - [x] 14.7 Write property test for merge window and count cap
    - **Property 19: Night_Event merge window and count cap**
    - **Validates: Requirements 13.9, 13.10**

  - [x] 14.8 Write property test for Night_Event ordering
    - **Property 20: Night_Event ordering**
    - **Validates: Requirements 13.12**

  - [x] 14.9 Write unit tests for event detection
    - Each event type; magnitudes; labels (for example "02:17 restless period"); environmental re-arm after an event; no-stage session; empty event list
    - _Requirements: 13.1–13.16, 18.1_

- [x] 15. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 16. Implement the Sample_Data_Generator and Sample_Dataset
  - [x] 16.1 Implement the synthetic night model
    - `backend/domain/sample_dataset.py`: the `SAMPLE_TIMEZONE` constant (one IANA zone, on a non-DST night) and `SAMPLE_RANDOM_SEED`
    - `tools/sample_data_generator.py`: using `random.Random(seed)`, generate one 8 h `stages` main-sleep log with 4–6 cycles (rem runs ≥5 min, ≥60 min apart; more deep early, more rem late), ≥2 awake runs of 5–20 min more than 15 min apart, and ≥3 Brief_Awakenings of 30–90 s. Also generate: 5 s integer heart rate at 45–80 with deep mean ≥5 bpm below awake mean, one 10–30 min gap, ≥90% coverage; 5-min rmssd at 20–90; per-minute steps with ≥3 bursts and ≥1 restless period; 1-min SensorPush readings with humidity 35–60%, a ≥1.0 °C / ≤30 min temperature change, a ≥1.0 hPa / ≤3 h pressure change, and a 20–60 min gap overlapping neither
    - Include a `validate_night()` self-check (using the Feature_Extractor at 3 min for the movement criteria) that fails generation when a target is missed
    - _Requirements: 14.2, 14.3, 14.4, 14.5, 14.6, 14.7, 14.13_

  - [x] 16.2 Implement deterministic file writers and CLI entry point
    - In `tools/sample_data_generator.py`: write `sleep-`, `heart_rate-`, `steps-` JSON, the `Heart Rate Variability Details - ` CSV, and a SensorPush CSV with unit-bearing headers, using `\n` newlines, fixed decimal formatting, and fixed key order for byte-identical output on Windows and Linux. CLI: `python -m tools.sample_data_generator --seed N --out DIR`
    - _Requirements: 14.1, 14.8_

  - [x] 16.3 Generate and commit the Sample_Dataset
    - Run the generator with `SAMPLE_RANDOM_SEED` into `sample_data/` and commit the files
    - _Requirements: 14.1, 14.8_

  - [x] 16.4 Write property test for generator determinism
    - **Property 21: Sample_Data_Generator determinism**
    - **Validates: Requirements 14.8**
    - ≥5 examples; also compares documented-seed output to the committed `sample_data/` bytes

  - [x] 16.5 Write property test for the generate-write-import round trip
    - **Property 22: Sample data generate-write-import round trip**
    - **Validates: Requirements 14.9**
    - ≥5 examples; import with the Sample_Timezone as Source_Timezone and compare timestamps to the second, values within half a unit of the last written decimal, and Stage_Segments exactly

  - [x] 16.6 Write unit tests for Sample_Dataset structural targets
    - Assert each target in 14.2–14.7 and 14.13 against the committed Sample_Dataset
    - _Requirements: 14.2, 14.3, 14.4, 14.5, 14.6, 14.7, 14.13_

- [x] 17. Wire the pipeline together
  - [x] 17.1 Implement the pipeline service layer
    - `backend/api/pipeline.py` (the only package allowed to import both ingestion and processing): at startup, check the Data_Directory, resolve the Display_Timezone, and register both adapters
    - `import_files(source_id, files, tz_overrides)`: run the registry import, reload all persisted telemetry (restart-safe; unreadable imports raise `PERSISTED_DATA_UNREADABLE`), dedup/merge/select sessions, keep only Fitbit intraday/HRV telemetry from files inside the selection date window, attach telemetry, attach Session_HRV when there's no in-session rmssd, and add missing-metric warnings. Return the Import_Report and candidate list
    - `use_sample_data()`: import `sample_data/` with the Sample_Timezone for every file whose default is Display_Timezone
    - `process(target_duration)`: align (passing nearby heart_rate points), run compression, extract features, and detect events. Raise `INSUFFICIENT_DATA` when the selected session has no stage, heart rate, or environmental data. A failure leaves stored state unchanged
    - _Requirements: 1.7, 1.10, 4.3, 4.5, 4.6, 7.4, 7.5, 8.1, 8.7, 8.10, 8.11, 14.11, 16.3, 16.5_

  - [x] 17.2 Write integration test for adapter extensibility
    - Register a fake adapter and check that its points and candidate sessions flow through the Session_Detector → Aligner → Feature_Extractor → Event_Detector with no changes to those modules
    - _Requirements: 7.2, 7.4_

  - [x] 17.3 Write integration test for the Sample_Dataset end-to-end pipeline
    - `use_sample_data()` gives exactly one 8 h candidate and an Import_Report with zero skipped files, values, and rows and no warnings; `process(180)` returns a Feature_Series, Coarse_States, and a non-empty event list
    - _Requirements: 14.11, 14.12, 7.5_

  - [x] 17.4 Write local-first and network-disabled test
    - A fixture that blocks `socket` connections; run import and processing with no credentials set and assert the results are the same as without the fixture
    - _Requirements: 7.5, 18.6_

  - [x] 17.5 Write error-structure and atomicity tests
    - Trigger each error code; assert a stable code, a description with no traceback, exception type, or source reference, a file name where applicable, and an action; a partial import keeps the valid files and lists the invalid ones; a failed operation leaves prior imports, sessions, and settings unchanged
    - _Requirements: 16.1, 16.2, 16.3, 16.4, 16.5_

  - [x] 17.6 Write storage-boundary test
    - Run the full pipeline against a temporary Data_Directory and assert that every created file and database lies inside it and that committed `sample_data/` files are unmodified
    - _Requirements: 15.1, 18.7_

- [x] 18. Final checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise; confirm `python -m pytest` finishes within 5 minutes.

## Completion evidence

All data-pipeline tasks are implemented and verified. The suite includes all 22
design properties, the real-file sample round trip, adapter extensibility,
restart and schema migration checks, rollback/error checks, storage and logging
checks, and offline execution. The two previous DST expected failures now pass.

- Verification: `python -m pytest` — 787 tests pass in about 25 seconds.
- The same suite passes with socket connection and DNS operations blocked.
- The committed seed produces byte-identical sample files; importing the sample
  yields one eight-hour candidate with no skipped files, rows, values, or warnings.
- `Pipeline.process(180)` yields 360 windows, 360 coarse states, and 12 events.
- HTTP, sonification, audio rendering, and the browser UI remain in the other specs.

## Notes

- Tasks marked with `*` are optional and can be skipped for a faster MVP. Requirement 18 (test coverage per area, one property test per invariant or round-trip criterion) is only fully met when those sub-tasks are implemented. The dependency test (1.2) is required by Requirement 17.3 and is not optional.
- Each property test uses Hypothesis with `@settings(max_examples=100)` (5 for Properties 21 and 22), keeps its strategies local to its own test file to avoid shared-file conflicts, and carries the tag `# Feature: sleep-replay-data-pipeline, Property N: <text>`.
- Implementation note on timezones: the Importers must apply Source_Timezones and DST rules, but Dependency_Rule (b) forbids ingestion → processing imports. So the stdlib-only localization helpers go in `backend/domain/timezones.py` (task 4.1), and `backend/processing/timezones.py` keeps Display_Timezone resolution and UTC conversion (task 4.2).
- Orchestration (import → persist → select → align → extract → detect) lives in `backend/api/pipeline.py` because only `api` may depend on both ingestion and processing. The HTTP Backend_API and the CLI in later specs call this layer.
- Documentation deliverables (`docs/data-formats.md`, the adapter guide, troubleshooting codes, README test command) are tracked in sleep-replay-app per design.md. This plan puts the adapter contract in docstrings (task 2.5) and keeps error codes in one table (task 2.4) for that documentation to reference.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1"] },
    { "id": 1, "tasks": ["1.2", "2.1", "2.2", "2.4", "4.1"] },
    { "id": 2, "tasks": ["2.3", "2.5", "2.6", "3.1", "4.2", "7.1", "8.1"] },
    { "id": 3, "tasks": ["2.7", "3.2", "3.5", "4.3", "7.2", "8.2", "8.3", "11.1", "12.1"] },
    { "id": 4, "tasks": ["3.3", "3.4", "5.1", "7.3", "7.4", "7.5", "8.4", "9.1", "11.2", "12.2", "12.3", "12.4", "13.1"] },
    { "id": 5, "tasks": ["3.6", "5.2", "7.6", "7.7", "8.5", "9.2", "9.3", "11.3", "11.4", "11.5", "11.6", "11.7", "11.8", "13.2", "14.1"] },
    { "id": 6, "tasks": ["7.8", "7.9", "9.4", "13.3", "14.2"] },
    { "id": 7, "tasks": ["9.5", "9.6", "13.4", "13.5", "13.6", "13.7", "13.8", "14.3"] },
    { "id": 8, "tasks": ["14.4", "16.1"] },
    { "id": 9, "tasks": ["14.5", "14.6", "14.7", "14.8", "14.9", "16.2"] },
    { "id": 10, "tasks": ["16.3", "17.1"] },
    { "id": 11, "tasks": ["16.4", "16.5", "16.6", "17.2", "17.3", "17.4", "17.5", "17.6"] }
  ]
}
```
