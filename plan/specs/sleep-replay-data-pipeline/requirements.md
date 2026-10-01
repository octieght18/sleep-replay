# Requirements Document

## Introduction

Sleep Replay is an open-source, local-first application that turns one night of sleep and bedroom-environment telemetry into a short ambient audio composition (30 seconds to 10 minutes, default 3 minutes). After waking, the user imports a Fitbit export and a SensorPush CSV. The application discovers the night's sleep session, aligns the datasets on a common timeline, extracts features and events, maps them to a small set of interacting musical dimensions, and renders a WAV file. Sleep Replay is not a medical device, not a diagnostic tool, and not an alarm.

This spec, sleep-replay-data-pipeline, is the first of three specs split from the Sleep Replay MVP requirements. It covers the domain model, Fitbit and SensorPush import, sleep session discovery and selection, timezone handling, alignment and resampling, the night compression time mapping, feature extraction, event detection, the synthetic sample data, the local storage foundation, structured errors and warnings, and the architecture rules. Its result is normalized, aligned, feature-extracted data plus detected Night_Events, verified by automated tests, with no audio and no UI. The specs sleep-replay-sonification and sleep-replay-app build on it. The design phase states the chosen architecture and confirms the input-format assumptions below before implementation begins.

## Scope

### In Scope (this spec)

- Normalized telemetry model and sleep session model (Requirements 1 and 2).
- Fitbit_Export import (sleep, heart rate, HRV, steps; individual files or one zip archive) and SensorPush_CSV import (Requirements 3 to 6).
- Data_Source_Adapter interface and its documentation (Requirement 7).
- Sleep session discovery, deduplication, merging, selection, and manual time ranges (Requirement 8).
- Timezone handling, alignment, and resampling onto the Aligned_Timeline (Requirements 9 and 10).
- Night compression time mapping, Compression_Ratio, and Target_Duration validation (Requirement 11).
- Feature extraction, Coarse_States, and Night_Event detection (Requirements 12 and 13).
- Synthetic Sample_Dataset and Sample_Data_Generator (Requirement 14).
- Data_Directory and local persistence foundation, log content restrictions (Requirement 15).
- User_Error structure and Import_Report / Processing_Report warnings (Requirement 16).
- Backend package structure and Dependency_Rules (Requirement 17).
- Automated tests for the data pipeline (Requirement 18).

### Out of Scope (this spec)

- Specified in sleep-replay-sonification: Mapping_Config parsing, validation, and printing; Soundscape_Presets; heart rate, HRV, movement, and environmental sound mappings; parameter smoothing; audio rendering and output; determinism of Replays; the Replay_Manifest and Manifest_Serializer; graceful degradation; the CLI; the example Replay WAV file. This includes the night compression criteria on Sonification_Engine inputs, on excluding raw telemetry from audio, and on the WAV frame count.
- Specified in sleep-replay-app: the Backend_API; the Import_View, Main_Screen, Playback_Timeline, and Settings_Panel; the Frontend display of Night_Times, the Display_Timezone, and timezone warnings; the complete end-to-end user flow through the Frontend; privacy and network restrictions; the "Use sample data" action shown on first start; setup and Docker; the remaining Documentation deliverables.
- Where a criterion of this spec names the Backend_API, Frontend, Settings_Panel, CLI, Replay, or Replay_Manifest, this spec specifies the data-pipeline behavior; the named interface or artifact is specified in the spec listed above.

### Non-Goals (MVP)

The MVP excludes:

- User accounts, authentication providers, multi-user support.
- Cloud infrastructure, cloud databases, hosted deployment.
- Live Fitbit / Google Health API integration.
- SensorPush cloud API integration (documented extension point only).
- Live wearable streaming, real-time audio, live soundscape.
- AI/LLM analysis and any transmission of data to external AI services.
- Medical diagnosis, medical interpretation, health recommendations.
- Alarm or wake-up functionality.
- Social features, subscriptions, billing.
- Mobile apps.
- Complex analytics, full health dashboards, elaborate visualizations, a sophisticated visual mapping editor.
- MP3/OGG output (WAV only for the MVP).
- Adapters for Apple Health, Google Health API, Oura, WHOOP, Garmin, Home Assistant, MQTT, additional SensorPush sensors, generic CSV, generic JSON (future extension points).
- Dedicated output modes for live soundscape, multi-day replay, room-only replay, wearable-only replay (future). Graceful degradation, specified in sleep-replay-sonification (Graceful Degradation), may still produce a replay when one data source is absent.

## Input Format Assumptions

These assumptions define the formats the MVP supports. The design phase confirms them, and `docs/data-formats.md` documents them for users.

### Fitbit export (Google Takeout / Fitbit account data export)

Accepted as individual files or as one zip archive. Files are matched by file name (case-insensitive) anywhere in the archive.

| File name pattern | Contents | Fields used | Default Source_Timezone |
|---|---|---|---|
| `sleep-YYYY-MM-DD.json` | JSON array of sleep logs | `logId`, `dateOfSleep`, `startTime`, `endTime`, `duration`, `type` (`stages` or `classic`), `isMainSleep`, `levels.data[]` and `levels.shortData[]` entries with `dateTime`, `level`, `seconds` | Display_Timezone (timestamps carry no offset) |
| `heart_rate-YYYY-MM-DD.json` | JSON array of intraday heart rate samples | `dateTime` (`MM/DD/YY HH:MM:SS`), `value.bpm` | UTC |
| `steps-YYYY-MM-DD.json` | JSON array of per-minute step counts | `dateTime` (`MM/DD/YY HH:MM:SS`), `value` (string or number) | UTC |
| `Heart Rate Variability Details - YYYY-MM-DD.csv` | HRV samples (typically 5-minute intervals) | `timestamp`, `rmssd` (other columns ignored) | Display_Timezone |
| `Daily Heart Rate Variability Summary - YYYY-MM(-DD).csv` | One HRV value per night | `timestamp` (date used), `rmssd` | Display_Timezone |

- Stage-style level names: `deep`, `light`, `rem`, `wake`. Classic-style level names: `asleep`, `restless`, `awake`.
- `levels.shortData` entries denote brief wake periods that override the underlying `levels.data` level for their duration.
- The UTC default for intraday heart rate and steps is an assumption based on known export behavior. The user can override every Source_Timezone at import, and the Aligner warns when the data overlap suggests a mismatch (Requirement 9).
- All other files in the export are ignored and listed as skipped.

### SensorPush CSV (SensorPush app or web dashboard export, HTP.xw)

- Delimiter: comma or semicolon; optional UTF-8 byte order mark; one header row.
- Columns are identified by case-insensitive header keywords:

| Column | Header keywords | Units recognized in header |
|---|---|---|
| Timestamp (required) | `timestamp`, `observed`, `date`, `time`, `datetime` | n/a |
| Temperature | contains `temp` and not `dew` | `°F`, `F`, `Fahrenheit`, `°C`, `C`, `Celsius` |
| Relative humidity | `humidity`, `rh` (not `absolute`) | `%` |
| Barometric pressure | `pressure`, `baro` | `inHg`, `mbar`, `hPa`, `kPa` |
| Sensor identifier (optional) | `sensor id`, `sensorid`, `sensor`, `device` | n/a |

- Ignored columns: dew point, VPD, and any other unrecognized columns.
- Timestamp formats: ISO 8601 with or without seconds and with or without offset (`YYYY-MM-DD HH:MM[:SS]`, `YYYY-MM-DDTHH:MM[:SS][±HH:MM|Z]`); US formats `M/D/YYYY h:mm[:ss] AM/PM` and `M/D/YYYY HH:MM[:SS]`. Day-first dates are unsupported in the MVP.
- Default Source_Timezone: Display_Timezone when no offset is present; an explicit offset takes precedence.
- Unit inference when a header states no unit: temperature median above 45 → °F, otherwise °C; pressure median between 25 and 32 → inHg, between 90 and 110 → kPa, between 900 and 1100 → hPa.

## Technology and Environment Constraints

- Backend: Python 3.11 or later with FastAPI; NumPy/Pandas for telemetry processing; SciPy where useful for filtering and interpolation; a practical open-source audio synthesis approach selected in the design.
- Frontend: a lightweight browser UI (React/Next.js or lighter) with all assets served locally.
- Local metadata and configuration storage: SQLite.
- Audio output: WAV. MP3/OGG may be added later.
- Setup: `docker compose up`, plus a documented non-Docker setup on Windows PowerShell (Python virtual environment and a simple frontend setup).
- Dependencies pinned to exact versions; repository published under an open-source license.
- Suggested layout (`backend/ingestion/{fitbit,sensorpush}`, `backend/domain`, `backend/processing`, `backend/sonification`, `backend/audio`, `backend/api`, `frontend/`, `tests/`, `sample_data/`, `docs/`, `README.md`, `docker-compose.yml`) may be refined in the design while preserving separation of concerns.

## Default Parameter Values

These are MVP starting points referenced by the requirements. Implementation may tune them for sound quality; final values are recorded in the documentation and the example Mapping_Config.

| Parameter | Default |
|---|---|
| Timeline_Resolution candidates | 1, 5, 10, 15, 30, 60 s |
| Max_Interpolation_Gap | heart_rate 5 min; hrv_rmssd 15 min; temperature, humidity, pressure 15 min |
| Smoothing window (Night_Time) | heart_rate 5 min; hrv_rmssd 15 min; temperature, humidity, pressure 15 min |
| Feature_Window_Span | 0.5 s of Replay_Time |
| Minimum_State_Duration | 1.0 s of Replay_Time |
| Wake_Event_Min_Duration | 5 min of Night_Time |
| Major_Transition_Min_Duration | 10 min of Night_Time |
| Movement_Transient_Threshold | movement intensity 0.1 |
| Movement_Burst_Threshold | movement intensity 0.6 |
| Restless_Threshold / Restless_Min_Duration | movement intensity 0.3 / 10 min of Night_Time |
| Environmental change thresholds | temperature ≥ 1.0 °C within 30 min; humidity ≥ 5 percentage points within 30 min; pressure ≥ 1.0 hPa within 3 h |
| Same-type event merge window | 15 min of Night_Time |
| Max_Event_Count | 12 |
| Default Random_Seed | a fixed integer documented in the example Mapping_Config |
| Max_Upload_Size | 2 GB per uploaded file |
| Max_Archive_Member_Size | 200 MB uncompressed per archive member |
| Display_Units | imperial (°F, inHg) by default; metric (°C, hPa) selectable |

The remaining parameters (Hysteresis_Threshold, normalization minimum spans, Stage_Crossfade_Duration, pulse rate range, Max_Transients_Per_Second, parameter rate limits, pressure maximum contribution, Restless_Texture_Boost, audio format, fade-in / fade-out, loudness) are defined in sleep-replay-sonification.

## Glossary

This glossary is the shared vocabulary referenced by sleep-replay-sonification and sleep-replay-app.

- **Sleep_Replay**: The complete MVP application (Backend, Frontend, CLI) running on the user's local machine.
- **Backend**: The local Python service that performs import, processing, sonification, and audio rendering, and hosts the Backend_API.
- **Backend_API**: The local HTTP interface used by the Frontend to communicate with the Backend.
- **Frontend**: The browser-based user interface consisting of the Import_View, Main_Screen, Settings_Panel, and Playback_Timeline.
- **Import_View**: The Frontend screen for importing Fitbit_Export files, SensorPush_CSV files, or the Sample_Dataset.
- **Main_Screen**: The Frontend screen showing the selected SleepSession, Generate Replay and Play controls, and the Playback_Timeline.
- **Settings_Panel**: The basic Frontend panel for editing Mapping_Config values and display settings.
- **Playback_Timeline**: The horizontal timeline on the Main_Screen spanning 00:00 to the Target_Duration, with a playhead and Night_Event markers.
- **CLI**: The command-line interface for generating sample data and Replays without the Frontend.
- **Importer**: Any component implementing the Data_Source_Adapter interface; the MVP Importers are the Fitbit_Importer and the SensorPush_Importer.
- **Fitbit_Importer**: The Importer that reads a Fitbit_Export.
- **SensorPush_Importer**: The Importer that reads a SensorPush_CSV.
- **Data_Source_Adapter**: The interface an Importer implements: `load(source) -> list[TelemetryPoint]`, plus an optional operation returning candidate SleepSessions.
- **Fitbit_Export**: Fitbit data exported through Google Takeout or the Fitbit account data export, in the file formats listed in the Input Format Assumptions, provided as individual files or one zip archive.
- **SensorPush_CSV**: A CSV file exported from the SensorPush app or web dashboard in the format listed in the Input Format Assumptions.
- **TelemetryPoint**: A normalized measurement `(timestamp, source, metric, value, unit)` with a timezone-aware timestamp.
- **Metric**: A normalized metric name. MVP metrics: `heart_rate`, `hrv_rmssd`, `steps`, `temperature`, `humidity`, `pressure`.
- **Continuous_Metric**: A metric that may be interpolated across short gaps: `heart_rate`, `hrv_rmssd`, `temperature`, `humidity`, `pressure`.
- **Canonical_Unit**: The internal unit of each metric: heart_rate bpm; hrv_rmssd ms; steps steps per minute; temperature °C; humidity percent; pressure hPa.
- **Display_Units**: The unit system used for showing environmental values in the Frontend (imperial or metric).
- **Sleep_Stage**: One of `awake`, `light`, `deep`, `rem`, `asleep`, `restless`, `unknown`.
- **Stage_Name_Mapping**: The mapping from source stage names to Sleep_Stage: `wake`→awake, `awake`→awake, `light`→light, `core`→light, `deep`→deep, `rem`→rem, `asleep`→asleep, `restless`→restless, any other name→unknown.
- **Stage_Segment**: A time interval `(start_time, end_time, stage)` with one Sleep_Stage.
- **Brief_Awakening**: An awake Stage_Segment originating from Fitbit `levels.shortData`.
- **SleepSession**: A normalized sleep period `(start_time, end_time, source, stages, telemetry)`, where `stages` is a list of Stage_Segments and `telemetry` is the list of TelemetryPoints within the period.
- **Session_HRV**: A single HRV value attached to a whole SleepSession, taken from a daily HRV summary.
- **Stage_Data**: At least one Stage_Segment of the SleepSession with a Sleep_Stage other than unknown. SleepSessions created from a manual time range, and Fitbit sleep logs without stage information, have no Stage_Data.
- **Session_Detector**: The component that discovers, deduplicates, merges, and selects candidate SleepSessions.
- **Aligner**: The component that converts timezones and units, sorts, deduplicates, and resamples telemetry onto the Aligned_Timeline.
- **Aligned_Timeline**: A regular sequence of samples from SleepSession start_time to end_time at the Timeline_Resolution, holding per-metric values, a Missing_Data_Mask, and a Sleep_Stage per sample.
- **Timeline_Resolution**: The sample spacing of the Aligned_Timeline, chosen from the Timeline_Resolution candidates.
- **Sample_Interval**: The half-open time interval of an Aligned_Timeline sample, from the sample timestamp (inclusive) to the next sample timestamp (exclusive); the last Sample_Interval ends at the SleepSession end_time.
- **Stage_Run**: A maximal run of consecutive Aligned_Timeline samples with the same Sleep_Stage; its duration is the sample count multiplied by the Timeline_Resolution.
- **Missing_Data_Mask**: A per-sample, per-metric flag that is true where no measured or validly interpolated value exists.
- **Max_Interpolation_Gap**: The longest gap between measured values of a Continuous_Metric across which the Aligner interpolates.
- **Processing_Report**: A summary of duplicates, gaps, sensor selection, and warnings produced during alignment and processing.
- **Import_Report**: A summary of an import: accepted files, skipped files with reasons, skipped value and row counts, per-metric counts and time coverage, and warnings. The Import_Report excludes telemetry values.
- **Night_Time**: Wall-clock time within the SleepSession.
- **Replay_Time**: Time position within the Replay, from 0 to the Target_Duration.
- **Target_Duration**: The selected Replay length: 30 s, 2 min, 3 min, 5 min, or 10 min.
- **Compression_Ratio**: The SleepSession duration divided by the Target_Duration.
- **Feature_Extractor**: The component that computes Feature_Series and Coarse_States over Feature_Windows.
- **Feature_Window**: A contiguous span of Night_Time that maps to one Feature_Window_Span of Replay_Time.
- **Feature_Series**: The per-Feature_Window features (mean, minimum, maximum, slope, missing fraction, smoothed values, stage fractions, movement intensity).
- **Movement_Intensity**: A per-Feature_Window movement value between 0.0 and 1.0.
- **Movement_Run**: A maximal run of consecutive Feature_Windows whose Movement_Intensity is at or above a given threshold; its duration is the total Night_Time span of those Feature_Windows.
- **Restless_Period**: A maximal contiguous run of Feature_Windows whose Movement_Intensity is at or above Restless_Threshold and whose combined Night_Time span is at least Restless_Min_Duration.
- **Coarse_State**: The smoothed Sleep_Stage used both for the soundscape and for the state label on the Main_Screen.
- **Event_Detector**: The component that detects Night_Events.
- **Night_Event**: A detected event with type, Night_Time, Replay_Time, magnitude (0.0–1.0), and label.
- **Event_Vocabulary**: The fixed descriptive event descriptions: "sleep onset", "awake", "awakening", "light sleep", "deep sleep", "REM", "movement", "restless period", "temperature rising", "temperature falling", "humidity rising", "humidity falling", "pressure rising", "pressure falling".
- **Mapping_Config**: The user-editable sonification configuration in YAML or JSON with per-metric target and sensitivity, optional smoothing and hysteresis values, and optional global target_duration and random_seed. Specified in sleep-replay-sonification.
- **Sound_Parameter**: A normalized control dimension in the range 0.0 to 1.0. Mapping targets: `pulse_rate`, `rhythmic_density`, `intensity`, `transient_density`, `brightness`, `texture_density`, `modulation`, or `none`. The soundscape state is driven by the Coarse_State and is not a mapping target.
- **Sonification_Engine**: The component that converts Feature_Series, Coarse_States, and Night_Events into Sound_Parameter trajectories and scheduled Transient_Events.
- **Transient_Event**: A short sound event triggered by movement or a Brief_Awakening.
- **Audio_Renderer**: The component that synthesizes Replay audio from Sound_Parameter trajectories and Transient_Events.
- **Replay**: A generated WAV file together with its Replay_Manifest.
- **Replay_Manifest**: The JSON metadata of a Replay used for timeline synchronization and reproducibility.
- **Input_Fingerprint**: A SHA-256 hash of the normalized TelemetryPoints and Stage_Segments of the selected SleepSession.
- **Random_Seed**: The integer seed for all procedural generation.
- **Metadata_Store**: The local SQLite database holding import records, sessions, settings, and Replay records.
- **Data_Directory**: The local directory holding imported files, normalized telemetry, Replays, and the Metadata_Store.
- **Source_Identifier**: A unique, stable name of a registered Data_Source_Adapter (for example `fitbit` or `sensorpush`), used as the `source` of the TelemetryPoints and SleepSessions it produces.
- **Source_Timezone**: The timezone used to interpret timestamps without offset for a given file type.
- **Display_Timezone**: The IANA timezone used to display Night_Times.
- **Sample_Dataset**: The synthetic 8-hour night included in the repository in Fitbit_Export and SensorPush_CSV formats.
- **Sample_Data_Generator**: The deterministic tool that produces the Sample_Dataset files.
- **Sample_Timezone**: The single IANA timezone, stated in the Documentation, in which the Sample_Dataset's offset-free timestamps are written.
- **User_Error**: A structured error with a stable code, a plain-language description, the affected file name where applicable, and the action the user needs to take.
- **Repository**: The Sleep Replay source repository.
- **Documentation**: The README and the files in `docs/`.
- **Test_Suite**: The automated tests of the Repository.
- **Reference_Machine**: A computer with 4 CPU cores, 8 GB RAM, and SSD storage running Windows 11 or Linux.
- **Dependency_Rules**: The Backend package import restrictions (a) to (e) stated in Requirement 17, criterion 2.

## Requirements

### Requirement 1: Normalized Telemetry Model

**User Story:** As a developer, I want every measurement normalized into one telemetry model, so that processing and sonification stay independent of device formats and new sources only need an adapter.

#### Acceptance Criteria

1. THE Importer SHALL represent each imported heart rate, HRV details, steps, temperature, humidity, and pressure value as exactly one TelemetryPoint containing timestamp, source, metric, value, and unit, where source identifies the originating Importer plus the sensor identifier when the file provides one (sleep stages and daily HRV summary values are represented as Stage_Segments and Session_HRV instead).
2. THE Importer SHALL assign to every TelemetryPoint a timezone-aware timestamp carrying an explicit UTC offset that is a whole number of seconds, taken from the offset in the source timestamp or, when the source timestamp has no offset, from the Source_Timezone of the file.
3. THE Importer SHALL assign to every TelemetryPoint exactly one Metric: heart_rate, hrv_rmssd, steps, temperature, humidity, or pressure.
4. THE Importer SHALL record in every TelemetryPoint the value as parsed from the source file without unit conversion, together with the unit stated in the column header, inferred by the unit inference rules in the Input Format Assumptions, or implied by the Fitbit file type, expressed as one of: bpm for heart_rate; ms for hrv_rmssd; steps per minute for steps; °C or °F for temperature; percent for humidity; hPa, mbar, inHg, or kPa for pressure.
5. IF an imported value is empty, not parseable as a decimal number (numeric strings such as a Fitbit steps value of "12" are parseable), or parses to NaN, positive infinity, or negative infinity, THEN THE Importer SHALL create no TelemetryPoint for the value, increment the skipped-value count for the file in the Import_Report, and continue importing the remaining values of the same row, entry, and file.
6. THE Aligner, Feature_Extractor, Event_Detector, Sonification_Engine, and Audio_Renderer SHALL accept as input only TelemetryPoint, Stage_Segment, SleepSession, Aligned_Timeline, Feature_Series, Coarse_State, Night_Event, Mapping_Config, Sound_Parameter trajectories, Transient_Event, Target_Duration, and Random_Seed values, and SHALL NOT accept file paths, file contents, or Fitbit- or SensorPush-specific record structures.
7. WHEN an import completes, THE Backend SHALL persist the normalized TelemetryPoints of the import in the Data_Directory before returning the Import_Report, such that the TelemetryPoints can be loaded after a Backend restart without re-importing the source files.
8. WHEN a list of valid TelemetryPoints (including the empty list) is persisted and then loaded, THE Backend SHALL return a list of the same length and order in which each TelemetryPoint has the same timestamp instant, UTC offset, source, metric, unit, and exactly equal value as the original (round-trip property), where a valid TelemetryPoint has a timezone-aware timestamp whose UTC offset is a whole number of seconds, a non-empty source, a Metric, a finite value, and a unit listed in criterion 4.
9. IF writing the normalized TelemetryPoints of an import to the Data_Directory fails, THEN THE Backend SHALL return a User_Error indicating that the imported data could not be saved, retain no TelemetryPoints of the failed import in the Data_Directory, and leave the persisted TelemetryPoints of earlier imports unchanged.
10. IF the persisted TelemetryPoints of an import cannot be read or include a TelemetryPoint that is not valid as defined in criterion 8, THEN THE Backend SHALL return a User_Error naming the source files of the affected import and instructing the user to re-import them, and SHALL pass none of that import's TelemetryPoints to the Aligner.

### Requirement 2: Sleep Session Model

**User Story:** As a developer, I want a normalized sleep session model, so that stage information from any source is handled uniformly.

#### Acceptance Criteria

1. THE Backend SHALL represent each sleep period as a SleepSession containing start_time and end_time as timezone-aware timestamps, source identifying the Importer that produced the SleepSession, stages as a list of Stage_Segments (possibly empty), and telemetry as the imported TelemetryPoints whose timestamps are greater than or equal to start_time and less than or equal to end_time.
2. THE Backend SHALL represent the stages of a SleepSession as a list of Stage_Segments ordered by start_time, in which each Stage_Segment has an end_time later than its start_time, starts at or after the end_time of the preceding Stage_Segment, starts at or after the SleepSession start_time, and ends at or before the SleepSession end_time.
3. THE Importer SHALL convert source stage names to Sleep_Stage values using the Stage_Name_Mapping, matching names case-insensitively after removing leading and trailing whitespace.
4. IF a time interval of a SleepSession lies within no Stage_Segment, including every interval of a SleepSession whose stages list is empty, THEN THE Backend SHALL treat the interval as Sleep_Stage unknown.
5. THE Backend SHALL determine the sleep onset time of a SleepSession as the start_time of the first Stage_Segment whose Sleep_Stage is neither awake nor unknown.
6. THE Backend SHALL determine the final wake time of a SleepSession as the end_time of the last Stage_Segment whose Sleep_Stage is neither awake nor unknown.
7. IF a SleepSession has an end_time earlier than or equal to its start_time, THEN THE Backend SHALL exclude the SleepSession from the candidate SleepSessions, continue processing the remaining sleep logs of the same file, and add a warning naming the source file to the Import_Report.
8. IF two source stage intervals overlap, THEN THE Backend SHALL assign the overlapping interval to the Brief_Awakening where exactly one of the two is a Brief_Awakening, and otherwise to the later-starting interval (for equal start times, the one appearing later in the source file), keeping the non-overlapping parts of the other interval as separate Stage_Segments with their original Sleep_Stage and keeping each Brief_Awakening identifiable as a Brief_Awakening.
9. IF a source stage interval extends before the SleepSession start_time or after the SleepSession end_time, or has a duration of 0 seconds, THEN THE Backend SHALL clip the interval to the SleepSession start_time and end_time and discard any interval whose duration after clipping is 0 seconds or less.
10. IF a SleepSession contains no Stage_Segment whose Sleep_Stage is neither awake nor unknown, THEN THE Backend SHALL leave the sleep onset time and final wake time of that SleepSession undefined and retain the SleepSession as a candidate SleepSession.

### Requirement 3: Fitbit Sleep Import

**User Story:** As a Fitbit Sense owner, I want my exported sleep logs imported, so that the replay follows my actual sleep stages.

#### Acceptance Criteria

1. WHEN a Fitbit sleep file is imported, THE Fitbit_Importer SHALL create one candidate SleepSession per sleep log entry in the file's JSON array, with start_time and end_time taken from the entry's startTime and endTime interpreted in the Source_Timezone for sleep files.
2. WHEN a sleep log entry contains levels.data entries, THE Fitbit_Importer SHALL create one Stage_Segment per levels.data entry spanning from the entry's dateTime to dateTime plus the entry's seconds, clipped to the SleepSession start_time and end_time, and discard any levels.data entry lying entirely outside that range.
3. WHEN a sleep log entry contains levels.shortData entries, THE Fitbit_Importer SHALL insert each shortData entry as an awake Stage_Segment spanning from the entry's dateTime to dateTime plus the entry's seconds, clipped to the SleepSession start_time and end_time, and split each overlapped Stage_Segment so the portions before and after the shortData interval keep their original Sleep_Stage.
4. WHEN a sleep log entry contains levels.shortData, THE Fitbit_Importer SHALL mark each Stage_Segment created from shortData as a Brief_Awakening.
5. WHEN a sleep log entry has type stages or classic, THE Fitbit_Importer SHALL assign Sleep_Stages through the Stage_Name_Mapping, mapping the stage-type levels deep, light, rem, and wake to deep, light, rem, and awake, and the classic-type levels asleep, restless, and awake to asleep, restless, and awake.
6. IF a sleep log entry has no valid levels.data entries because the levels field is absent, levels.data is empty, or every levels.data entry was skipped, THEN THE Fitbit_Importer SHALL create the candidate SleepSession with one Stage_Segment of Sleep_Stage unknown spanning start_time to end_time and add a warning to the Import_Report stating that stage information is unavailable for the session.
7. IF a levels.data entry contains a level name absent from the Stage_Name_Mapping, THEN THE Fitbit_Importer SHALL assign Sleep_Stage unknown to the resulting Stage_Segment and add one warning per distinct level name per file to the Import_Report naming the level and the number of affected Stage_Segments.
8. THE Fitbit_Importer SHALL attach the logId and isMainSleep values of each sleep log entry to its candidate SleepSession for use by the Session_Detector.
9. IF a sleep log entry lacks startTime or endTime or either value cannot be parsed as a timestamp, THEN THE Fitbit_Importer SHALL skip the entry, add a warning naming the file and the entry's logId (where present) to the Import_Report, and continue importing the remaining entries of the file.
10. IF a levels.data or levels.shortData entry has a missing or unparseable dateTime, or a seconds value that is missing, non-numeric, or less than or equal to 0, THEN THE Fitbit_Importer SHALL skip the levels entry, increment the skipped-value count for the file in the Import_Report, and continue processing the remaining levels entries.
11. IF a sleep log entry lacks the isMainSleep field or its value is not a boolean, THEN THE Fitbit_Importer SHALL record isMainSleep as false for the candidate SleepSession.

### Requirement 4: Fitbit Heart Rate, HRV, and Steps Import

**User Story:** As a Fitbit Sense owner, I want my exported heart rate, HRV, and activity data imported, so that the replay reflects how my body changed through the night.

#### Acceptance Criteria

1. WHEN a Fitbit heart rate file is loaded, THE Fitbit_Importer SHALL create one heart_rate TelemetryPoint in bpm per array entry, taking the timestamp from dateTime in the format MM/DD/YY HH:MM:SS (two-digit years read as 2000 to 2099) and the value from value.bpm.
2. WHEN a Fitbit HRV details file is loaded, THE Fitbit_Importer SHALL create one hrv_rmssd TelemetryPoint in ms per data row, taking the timestamp from the timestamp column as an ISO 8601 date-time and the value from the rmssd column, and ignoring all other columns.
3. IF no hrv_rmssd TelemetryPoint lies between the start_time and end_time of the selected SleepSession and one or more daily HRV summary rows are dated on the calendar date of the selected SleepSession end_time in the Display_Timezone, THEN THE Fitbit_Importer SHALL attach the rmssd value of the matching row, or the mean rmssd when several rows match, to the selected SleepSession as the Session_HRV.
4. WHEN a Fitbit steps file is loaded, THE Fitbit_Importer SHALL create one steps TelemetryPoint in steps per minute per array entry, taking the timestamp from dateTime in the format MM/DD/YY HH:MM:SS (two-digit years read as 2000 to 2099) and the numeric value from value, whether value is stored as a string or a number.
5. WHEN a SleepSession becomes the selected SleepSession, including through automatic selection or a user choice, THE Fitbit_Importer SHALL load only the heart rate, steps, and HRV details files whose file-name date lies from one calendar day before the start_time date through one calendar day after the end_time date, with both dates taken in the Display_Timezone.
6. IF, for heart_rate, hrv_rmssd, or steps, no TelemetryPoint of that metric lies between the start_time and end_time of the selected SleepSession (and, for hrv_rmssd, no Session_HRV is attached), THEN THE Fitbit_Importer SHALL add one warning to the Import_Report naming that metric and continue the import without returning a User_Error.
7. IF a heart rate or steps entry has an unparseable dateTime value, or an HRV details or daily HRV summary row has an unparseable timestamp value, THEN THE Fitbit_Importer SHALL skip that entry or row, increment the skipped-row count for the file in the Import_Report, and continue reading the remaining entries of the file.
8. IF a heart_rate value lies outside 20 to 250 bpm, an hrv_rmssd or daily summary rmssd value is less than or equal to 0 ms or greater than 500 ms, or a steps value is negative or greater than 300 steps per minute, THEN THE Fitbit_Importer SHALL exclude the value and increment the skipped-value count for the file in the Import_Report.

### Requirement 5: Fitbit Export Packaging and Unsupported Files

**User Story:** As a user, I want to import my Fitbit export as individual files or a zip archive and get clear guidance when a file is not supported, so that I can fix import problems myself.

#### Acceptance Criteria

1. THE Fitbit_Importer SHALL accept a Fitbit_Export either as one or more individual files or as exactly one zip archive, where a zip archive is a provided file whose name ends in `.zip` (case-insensitive).
2. WHEN a Fitbit_Export is provided, THE Fitbit_Importer SHALL read only the individual files and zip archive members whose file name matches a Fitbit file name pattern listed in the Input Format Assumptions, comparing the final path component of the name case-insensitively, treating YYYY, MM, and DD as 4-digit, 2-digit, and 2-digit numbers, and matching archive members at any directory depth.
3. WHEN a zip archive is provided, THE Fitbit_Importer SHALL read archive member contents directly from the archive without creating any file at a path derived from an archive member name, including member names that are absolute paths or contain `..` segments.
4. IF the declared uncompressed size of a matching archive member exceeds Max_Archive_Member_Size, or decompressing the member produces more than Max_Archive_Member_Size bytes, THEN THE Fitbit_Importer SHALL stop decompressing the member, import no data from the member, record the member path and the size limit in the Import_Report, and continue with the remaining members.
5. IF an individual file or a non-directory archive member matches no supported Fitbit file name pattern, THEN THE Fitbit_Importer SHALL skip it, list its name (the full member path for archive members) under unsupported files in the Import_Report, and include the total count of unsupported files in the Import_Report.
6. IF no individual file or archive member matches a supported Fitbit file name pattern, THEN THE Fitbit_Importer SHALL import no data from the selection, leave previously imported data unchanged, and return a User_Error that lists every supported Fitbit file name pattern and states the steps for requesting a Fitbit export through Google Takeout.
7. IF a supported file or archive member is password-protected or cannot be decompressed, cannot be parsed as JSON or CSV, has a JSON top-level value other than an array, or is a CSV lacking a `timestamp` or `rmssd` header column, THEN THE Fitbit_Importer SHALL skip the file, record in the Import_Report the file name with the reason (password protection, decompression failure, line number of the first parse error, non-array top-level value, or name of the missing column), and continue importing the remaining files.
8. IF an entry of a supported JSON file lacks a field required for its file type (sleep: `startTime`, `endTime`; heart rate: `dateTime`, `value.bpm`; steps: `dateTime`, `value`), or an entry or CSV row has a timestamp that cannot be parsed, THEN THE Fitbit_Importer SHALL skip the entry or row, increment the skipped-row count for the file in the Import_Report, and continue with the remaining entries and rows of the file.
9. IF one Fitbit import provides more than one zip archive, or a zip archive together with individual files, THEN THE Fitbit_Importer SHALL import no data from the selection, leave previously imported data unchanged, and return a User_Error stating that either one zip archive or individual files are accepted and recommending selecting the single archive that contains the Fitbit data or the extracted Fitbit files.
10. IF a zip archive cannot be opened because it is corrupted, truncated, or not in zip format, THEN THE Fitbit_Importer SHALL import no data from the archive, leave previously imported data unchanged, and return a User_Error naming the archive file and recommending downloading the export again or selecting the extracted Fitbit files.

### Requirement 6: SensorPush CSV Import

**User Story:** As a SensorPush HTP.xw owner, I want to import the CSV exported from the SensorPush app, so that the replay reflects my bedroom temperature, humidity, and pressure.

#### Acceptance Criteria

1. WHEN a SensorPush_CSV file is provided, THE SensorPush_Importer SHALL create one TelemetryPoint for each parseable measurement value in each data row, with metric `temperature`, `humidity`, or `pressure`, the row's timestamp, a source identifying SensorPush, and the unit determined under criterion 3 or 4.
2. THE SensorPush_Importer SHALL identify the timestamp, temperature, relative humidity, barometric pressure, and sensor identifier columns by matching each header against the case-insensitive header keywords and exclusions listed in the Input Format Assumptions, and SHALL ignore all other columns, including dew point and VPD, without adding a warning.
3. THE SensorPush_Importer SHALL determine the unit of each measurement column from the unit tokens listed for that column in the Input Format Assumptions, recognizing the single-letter tokens `F` and `C` only when no letter directly precedes or follows them.
4. IF a measurement column header states no recognized unit, THEN THE SensorPush_Importer SHALL infer the unit from the median of that column's parseable values using the value-range rules in the Input Format Assumptions, treat humidity as percent, and add a warning naming the metric and the inferred unit to the Import_Report.
5. THE SensorPush_Importer SHALL accept comma-delimited and semicolon-delimited files with or without a UTF-8 byte order mark, SHALL exclude the byte order mark from the first column header, and SHALL ignore blank lines without counting them as skipped rows.
6. THE SensorPush_Importer SHALL parse timestamps in each format listed in the Input Format Assumptions, SHALL interpret slash-separated dates as month/day/year, SHALL apply an explicit offset when one is present, and SHALL otherwise interpret the timestamp in the SensorPush_CSV Source_Timezone (Display_Timezone by default).
7. IF a SensorPush_CSV has no recognizable timestamp column, or none of the temperature, relative humidity, and barometric pressure columns, THEN THE SensorPush_Importer SHALL return a User_Error listing the detected column headers and the expected header keywords, and SHALL create no TelemetryPoints from the file.
8. IF a non-blank data row has an empty timestamp or a timestamp matching none of the supported formats, THEN THE SensorPush_Importer SHALL skip the row, create no TelemetryPoints from it, and increase the skipped-row count in the Import_Report by 1.
9. IF a SensorPush_CSV contains no data rows, or none of its data rows yields a TelemetryPoint, THEN THE SensorPush_Importer SHALL return a User_Error stating that no readable rows were found and listing the supported timestamp formats, and SHALL create no TelemetryPoints from the file.
10. IF a SensorPush_CSV has a recognizable timestamp column and lacks one or two of the temperature, relative humidity, and barometric pressure columns, THEN THE SensorPush_Importer SHALL import the available measurement columns and add one warning per missing metric to the Import_Report stating that the metric's data is unavailable.
11. WHEN a SensorPush_CSV containing a sensor identifier column is imported, THE SensorPush_Importer SHALL include the row's sensor identifier in the source field of each TelemetryPoint created from that row, and SHALL use a source identifying SensorPush without a sensor identifier for rows whose sensor identifier cell is empty.
12. IF more than one column matches the same column type under criterion 2, THEN THE SensorPush_Importer SHALL use the leftmost matching column, ignore the other matching columns, and add a warning naming the ignored column headers to the Import_Report.
13. IF a data row has a parseable timestamp and a measurement cell that is empty or non-numeric, THEN THE SensorPush_Importer SHALL skip that value only, keep the row's other parseable values, and increase the skipped-value count in the Import_Report by 1 for each skipped value.
14. IF the barometric pressure header states no recognized unit and the median of its parseable values falls outside the inHg, kPa, and hPa ranges in the Input Format Assumptions, THEN THE SensorPush_Importer SHALL create no pressure TelemetryPoints from the file, import the remaining measurement columns, and add a warning to the Import_Report stating that the pressure unit could not be determined.

### Requirement 7: Extensible Data Source Adapters

**User Story:** As a contributor, I want a documented adapter interface, so that I can add sources such as a SensorPush cloud API adapter without touching processing or sonification.

#### Acceptance Criteria

1. THE Backend SHALL define a Data_Source_Adapter interface in which each adapter declares a Source_Identifier and provides a load operation that accepts a source (for the MVP Importers, one or more local file paths or one zip archive) and returns a list of TelemetryPoints, each with a timezone-aware timestamp, a `source` equal to the adapter's Source_Identifier, a Metric, a finite numeric value, and a unit convertible to that Metric's Canonical_Unit.
2. THE Backend SHALL define an optional Data_Source_Adapter operation that returns candidate SleepSessions, each with a timezone-aware start_time earlier than its end_time and Stage_Segments whose stages are Sleep_Stage values, and THE Session_Detector SHALL treat an adapter that does not implement this operation as contributing zero candidate SleepSessions.
3. THE Fitbit_Importer and the SensorPush_Importer SHALL each implement the Data_Source_Adapter load operation, and THE Fitbit_Importer SHALL also implement the candidate SleepSession operation.
4. WHEN a new Data_Source_Adapter is registered with the Backend, THE Backend SHALL accept imports for its Source_Identifier through the Backend_API and pass its TelemetryPoints and candidate SleepSessions to the Aligner and Session_Detector, with no source-code changes to the Aligner, Session_Detector, Feature_Extractor, Event_Detector, Sonification_Engine, or Audio_Renderer.
5. THE Sleep_Replay SHALL complete import and processing, from importing a Fitbit_Export and a SensorPush_CSV through session discovery, alignment, feature extraction, and event detection, using only local files, with no Fitbit, Google, or SensorPush cloud credentials configured, without prompting for credentials, and with no internet connection after installation.
6. IF a Data_Source_Adapter load operation fails for a source as a whole (unreadable, unsupported, or malformed), THEN THE Backend SHALL return a User_Error identifying the affected file where applicable, store no TelemetryPoints from that source, and leave previously imported data in the Metadata_Store and Data_Directory unchanged.
7. IF a TelemetryPoint returned by a load operation has a timestamp without timezone information, a metric outside the MVP Metrics, a non-finite value, or a unit that cannot be converted to the Metric's Canonical_Unit, THEN THE Backend SHALL exclude that TelemetryPoint, retain the remaining valid TelemetryPoints, and report the excluded count per reason in the Import_Report.
8. THE Documentation SHALL describe the Data_Source_Adapter interface, including both operations with their inputs and outputs, the TelemetryPoint fields, the MVP Metric names and Canonical_Units, the steps to register a new adapter, and the SensorPush cloud API adapter as an unimplemented extension point.

### Requirement 8: Sleep Session Discovery and Selection

**User Story:** As a user, I want the application to find last night's sleep automatically and let me pick another night, so that I can start listening with minimal effort.

#### Acceptance Criteria

1. WHEN an import completes, THE Session_Detector SHALL list each candidate SleepSession from all Importers across all completed imports that remains after deduplication (criterion 5) and merging (criterion 6). Each entry SHALL show its start time and end time in Display_Timezone, its duration in hours and minutes, and its stage-data availability (`stages`, `classic`, or `none`). Entries SHALL be ordered by start time from latest to earliest.
2. WHILE no user-chosen or manually defined SleepSession is selected, WHEN an import completes, THE Session_Detector SHALL select the candidate SleepSession with the latest start time among the candidates flagged as main sleep (Fitbit `isMainSleep` true).
3. IF automatic selection (criterion 2) finds no candidate SleepSession flagged as main sleep, THEN THE Session_Detector SHALL select the longest candidate SleepSession among the candidates whose end time falls on the latest end-time calendar date in Display_Timezone. When durations are equal, it SHALL choose the candidate with the later start time.
4. WHEN the user chooses a candidate SleepSession, THE Session_Detector SHALL make the chosen SleepSession the selected SleepSession, replacing any previous selection, including a manually defined SleepSession.
5. IF two candidate SleepSessions share a logId or have identical start and end times, THEN THE Session_Detector SHALL keep one candidate and discard the others, preferring the main-sleep candidate, then the candidate with `stages` data, then the candidate from the file name that sorts first, and SHALL record the number of discarded duplicates in the Import_Report.
6. IF two candidate SleepSessions remaining after deduplication overlap in time (each starts before the other ends; intervals that only touch do not overlap), THEN THE Session_Detector SHALL merge them into one SleepSession spanning the union of both intervals, using for the overlapping interval the Stage_Segments of the main-sleep candidate (or of the longer candidate when neither or both are main sleep, or of the earlier-starting candidate when durations are equal), keeping each candidate's own Stage_Segments outside the overlap, flagging the merged SleepSession as main sleep if either input was, repeating merging until no candidates overlap, and adding an Import_Report warning stating the start and end times of the merged candidates.
7. IF an import completes, no candidate SleepSession exists across all completed imports, and no manually defined SleepSession is selected, THEN THE Session_Detector SHALL return a User_Error stating that no sleep log was found and offering manual definition of a time range, and SHALL keep the imported telemetry.
8. WHEN the user defines a manual start time and end time that satisfy criterion 9, THE Session_Detector SHALL interpret both times in Display_Timezone and create a SleepSession spanning the range, with one Stage_Segment of Sleep_Stage unknown covering the whole range and no main-sleep flag, and SHALL make this SleepSession the selected SleepSession.
9. IF a manual time range has a missing or unparseable start or end time, an end time earlier than or equal to the start time, or a duration longer than 24 hours, THEN THE Session_Detector SHALL return a User_Error stating the valid range (end time after start time, duration at most 24 hours) and SHALL keep the current selection unchanged.
10. WHEN a SleepSession becomes the selected SleepSession, or an import completes while a SleepSession is selected, THE Session_Detector SHALL attach to that SleepSession every TelemetryPoint from all completed imports whose timestamp, compared as a timezone-aware instant, satisfies start_time ≤ timestamp ≤ end_time, and SHALL attach no other TelemetryPoints.
11. WHILE a user-chosen or manually defined SleepSession is selected, WHEN an import completes, THE Session_Detector SHALL keep that SleepSession selected, and where criterion 6 merged it with a new candidate, the merged SleepSession that contains it SHALL stay selected.

### Requirement 9: Timezone Handling

**User Story:** As a user whose devices record time differently, I want all timestamps brought to a common timezone, so that sleep and room data line up correctly.

#### Acceptance Criteria

1. WHEN an imported file contains timestamps without a timezone offset, THE Importer SHALL interpret those timestamps in the Source_Timezone applied to the file type and record each file type with its applied Source_Timezone in the Import_Report.
2. THE Importer SHALL apply as the Source_Timezone of each file type the IANA timezone the user supplies as an override for that file type at import, and otherwise the default Source_Timezone listed in the Input Format Assumptions, where a default of Display_Timezone means the Display_Timezone in effect when the import starts.
3. WHEN a timestamp contains an explicit offset (`±HH:MM` or `Z`), THE Importer SHALL use the explicit offset and ignore both the default and any user-supplied Source_Timezone for that timestamp.
4. IF a timestamp without offset is ambiguous (falls in a repeated hour) or nonexistent (falls in a skipped hour) in the applied Source_Timezone because of a daylight saving time transition, THEN THE Importer SHALL resolve an ambiguous time to its earlier UTC instant, shift a nonexistent time forward by the length of the transition gap (for example 02:30 becomes 03:30 for a 1-hour gap), and add one warning per affected file to the Import_Report stating the file name and the number of adjusted timestamps.
5. THE Aligner SHALL convert all timestamps to UTC before alignment, so that re-aligning an already-imported SleepSession under a different Display_Timezone produces an identical Aligned_Timeline and Input_Fingerprint.
6. THE Sleep_Replay SHALL set the Display_Timezone to the IANA timezone identifier supplied through a configuration setting read at Backend startup (settable both for `docker compose up` and for the non-Docker setup), otherwise to the host machine's timezone, and SHALL show the Display_Timezone in effect in the Settings_Panel (Frontend display is specified in sleep-replay-app).
7. THE Frontend SHALL display every Night_Time in the Display_Timezone using the UTC offset in effect at that instant, so that Night_Times after a daylight saving time transition within a SleepSession reflect the post-transition offset (Frontend display is specified in sleep-replay-app).
8. THE Aligner SHALL space Aligned_Timeline samples uniformly in UTC elapsed time and compute the SleepSession duration used for the Compression_Ratio from UTC elapsed time, so that a SleepSession from 22:00 to 06:00 local time lasts 9 hours across a 1-hour fall-back transition and 7 hours across a 1-hour spring-forward transition.
9. IF fewer than 50% of the Aligned_Timeline samples of the selected SleepSession have a heart_rate value (Missing_Data_Mask false) and at least one heart_rate TelemetryPoint lies within the 14 hours before the SleepSession start_time or within the 14 hours after its end_time, THEN THE Aligner SHALL add a warning to the Processing_Report stating the heart_rate coverage percentage, the Source_Timezone applied to heart_rate files, that this Source_Timezone is likely incorrect, and how to re-import with a different Source_Timezone, and SHALL continue processing without blocking Replay generation.
10. IF a user-supplied Source_Timezone override is not a valid IANA timezone identifier, THEN THE Importer SHALL reject the import with a User_Error naming the invalid value and the affected file type, import no files from that import, and leave previously imported data unchanged.
11. IF the configured Display_Timezone is not a valid IANA timezone identifier, or no Display_Timezone is configured and the host machine's timezone cannot be determined, THEN THE Sleep_Replay SHALL use UTC as the Display_Timezone and display a warning in the Frontend stating the reason and how to configure the Display_Timezone (Frontend display is specified in sleep-replay-app).

### Requirement 10: Alignment and Resampling

**User Story:** As a developer, I want a reusable alignment and resampling layer, so that sensors with different sampling rates, gaps, and timestamps share one timeline without inventing data.

#### Acceptance Criteria

1. THE Aligner SHALL sort the TelemetryPoints of each source and metric by ascending timestamp.
2. IF multiple TelemetryPoints share the same source, metric, and timestamp, THEN THE Aligner SHALL replace them with one TelemetryPoint whose value is the arithmetic mean of their Canonical_Unit values and record, per source and metric, the number of removed duplicate TelemetryPoints in the Processing_Report.
3. THE Aligner SHALL convert every TelemetryPoint value to the Canonical_Unit of its metric (°F to °C; inHg, kPa, and mbar to hPa) before deduplication, sensor selection, and resampling.
4. THE Aligner SHALL select as Timeline_Resolution the smallest Timeline_Resolution candidate that is greater than or equal to the shortest median interval between consecutive TelemetryPoints among the Continuous_Metrics that have at least two TelemetryPoints within the SleepSession after deduplication and sensor selection.
5. IF no Continuous_Metric has at least two TelemetryPoints within the SleepSession, or the shortest median interval exceeds 60 seconds, THEN THE Aligner SHALL use a Timeline_Resolution of 60 seconds.
6. THE Aligner SHALL produce an Aligned_Timeline whose sample timestamps are the SleepSession start_time plus each non-negative integer multiple of the Timeline_Resolution in elapsed time that is earlier than the SleepSession end_time.
7. WHEN one or more measured values of a Continuous_Metric have timestamps within the Sample_Interval of a sample, THE Aligner SHALL assign the arithmetic mean of those values to the sample.
8. WHEN no measured value of a Continuous_Metric lies within the Sample_Interval of a sample and the nearest measured values before and after the Sample_Interval are separated by no more than the Max_Interpolation_Gap of the metric, THE Aligner SHALL assign the value linearly interpolated between those two measured values at the sample timestamp.
9. IF no measured value of a Continuous_Metric lies within the Sample_Interval of a sample and the nearest measured values before and after the Sample_Interval are separated by more than the Max_Interpolation_Gap of the metric, THEN THE Aligner SHALL mark the sample as missing for that metric in the Missing_Data_Mask and record one Processing_Report entry per gap stating the metric, the timestamp of the last measured value before the gap, and the elapsed time between the two measured values.
10. IF a metric has no measured value within the SleepSession, or a Sample_Interval contains no measured value of the metric and lies entirely before the first or entirely after the last measured value of the metric, THEN THE Aligner SHALL mark the sample as missing for that metric in the Missing_Data_Mask without extrapolating, and record each metric without any measured value as unavailable in the Processing_Report.
11. THE Aligner SHALL assign each sample the Sleep_Stage of the Stage_Segment whose half-open interval from start_time (inclusive) to end_time (exclusive) contains the sample timestamp, or Sleep_Stage unknown when no Stage_Segment contains the sample timestamp, without interpolating between Sleep_Stages.
12. THE Aligner SHALL assign each steps value, unchanged in steps per minute and without interpolation, to every sample whose timestamp lies within the half-open interval from the steps TelemetryPoint timestamp (inclusive) to 60 seconds later (exclusive).
13. IF TelemetryPoints of the same temperature, humidity, or pressure metric within a SleepSession come from more than one source, THEN THE Aligner SHALL use only the TelemetryPoints of the source with the most TelemetryPoints for that metric within the SleepSession, choose the lexicographically first source identifier when counts are equal, and add a warning naming the metric, the selected source, and the ignored sources to the Processing_Report.
14. FOR ALL Aligned_Timelines, every sample value of a metric not marked missing SHALL lie between the minimum and maximum of the deduplicated, unit-converted measured values used for that metric within the SleepSession (invariant).
15. FOR ALL SleepSessions, the Aligned_Timeline sample count SHALL equal the SleepSession duration in elapsed seconds divided by the Timeline_Resolution in seconds, rounded up to the next integer (invariant).
16. IF a TelemetryPoint has a unit that cannot be converted to the Canonical_Unit of its metric, THEN THE Aligner SHALL exclude the TelemetryPoint from alignment and add a warning naming the source, metric, unit, and number of excluded TelemetryPoints to the Processing_Report.
17. IF a sample timestamp lies within no measured steps minute defined in criterion 12, THEN THE Aligner SHALL mark the sample as missing for steps in the Missing_Data_Mask.
18. FOR ALL SleepSessions, aligning the same TelemetryPoints and Stage_Segments supplied in any input order SHALL produce identical Aligned_Timelines and Processing_Reports (determinism property).

### Requirement 11: Night Compression Time Mapping

**User Story:** As a user, I want to choose how long my replay is, so that a whole night fits into a short listening session.

#### Acceptance Criteria

1. THE Sleep_Replay SHALL offer, in the Settings_Panel and in the CLI, exactly five Target_Duration values: 30 seconds, 2 minutes, 3 minutes, 5 minutes, and 10 minutes.
2. WHEN the Backend starts generating a Replay, THE Backend SHALL select the Target_Duration in this order of precedence: the Target_Duration specified in the generation request, then the target_duration of the active Mapping_Config, then 3 minutes.
3. WHEN the Backend generates a Replay, THE Backend SHALL compute the Compression_Ratio as the SleepSession duration (end_time minus start_time, in seconds) divided by the Target_Duration (in seconds), and record the Compression_Ratio with at least 3 decimal places of precision and the Target_Duration in seconds in the Replay_Manifest (for example, an 8-hour SleepSession with a 3-minute Target_Duration yields a Compression_Ratio of 160.000).
4. THE Backend SHALL map each Night_Time t from SleepSession start_time to end_time (inclusive) to Replay_Time = (t − start_time) / Compression_Ratio, so that start_time maps to 0 seconds and end_time maps to the Target_Duration, with every computed Replay_Time deviating from this formula by at most 1 millisecond.
5. THE Backend SHALL map Night_Time to Replay_Time such that FOR ALL pairs of Night_Times t1 < t2 within a SleepSession, the Replay_Time computed for t1 is strictly less than the Replay_Time computed for t2 (order-preservation property).
6. IF the SleepSession duration is less than or equal to the selected Target_Duration, THEN THE Backend SHALL reject the generation request with a User_Error that lists the offered Target_Duration values shorter than the SleepSession duration (or, when no offered value is shorter, states that the SleepSession is too short to replay), write no WAV file, and leave existing Replays and Replay records in the Metadata_Store unchanged.
7. IF a Replay generation request or a Mapping_Config specifies a Target_Duration other than 30, 120, 180, 300, or 600 seconds, THEN THE Backend SHALL reject it with a User_Error listing the five accepted values, generate no Replay, and keep the previously active Target_Duration unchanged.

### Requirement 12: Feature Extraction

**User Story:** As a developer, I want meaningful features computed over time windows, so that the soundscape follows trends rather than raw sensor noise.

#### Acceptance Criteria

1. THE Feature_Extractor SHALL divide the SleepSession into consecutive, non-overlapping Feature_Windows, where each Feature_Window except the last spans Compression_Ratio × Feature_Window_Span of Night_Time, the last Feature_Window ends at the SleepSession end_time, and each Feature_Window contains the Aligned_Timeline samples with timestamps at or after its start and before its end (the last Feature_Window also contains the sample at the SleepSession end_time).
2. THE Feature_Extractor SHALL compute, for each Continuous_Metric and each Feature_Window, the mean, minimum, and maximum of the samples not marked missing, the slope of the least-squares linear fit of those samples against Night_Time in Canonical_Unit per hour, and the missing-sample fraction as the number of samples marked missing in the Missing_Data_Mask divided by the number of samples in the Feature_Window.
3. IF a Feature_Window contains no sample of a Continuous_Metric that is not marked missing, including a Feature_Window that contains no samples, THEN THE Feature_Extractor SHALL mark the mean, minimum, maximum, and slope of that metric in that Feature_Window as missing and set its missing-sample fraction to 1.0.
4. THE Feature_Extractor SHALL compute, for each Continuous_Metric and each Feature_Window, a smoothed value equal to the mean of the samples not marked missing within a Night_Time interval of that metric's smoothing window length (heart_rate 5 min; hrv_rmssd, temperature, humidity, pressure 15 min) centered on the Feature_Window midpoint and truncated to the SleepSession start_time and end_time.
5. IF no sample of a Continuous_Metric that is not marked missing lies within the smoothing interval of a Feature_Window, THEN THE Feature_Extractor SHALL mark the smoothed value of that metric in that Feature_Window as missing.
6. THE Feature_Extractor SHALL compute, for each Feature_Window, the fraction of its Night_Time covered by each Sleep_Stage, with Brief_Awakenings taking precedence over overlapping Stage_Segments and Night_Time covered by no Stage_Segment counted as unknown, and SHALL select as the dominant Sleep_Stage the stage with the largest fraction, breaking ties in the fixed order awake, rem, light, deep, asleep, restless, unknown.
7. WHILE the SleepSession contains at least one steps sample not marked missing, THE Feature_Extractor SHALL compute each Feature_Window's Movement_Intensity from the mean steps per minute of its steps samples not marked missing, such that 0 steps per minute yields 0.0, a greater mean yields an equal or greater Movement_Intensity, every value lies between 0.0 and 1.0 inclusive, and a Feature_Window with no steps sample that is not marked missing yields 0.0.
8. IF the SleepSession contains no steps sample that is not marked missing, THEN THE Feature_Extractor SHALL set each Feature_Window's Movement_Intensity to the fraction of its Night_Time covered by restless Stage_Segments or Brief_Awakenings (0.0 when neither is present) and add a warning to the Processing_Report indicating that movement was derived from sleep stages.
9. THE Feature_Extractor SHALL produce one Coarse_State per Feature_Window by grouping consecutive Feature_Windows with equal dominant Sleep_Stage into runs and, in chronological order, merging each run whose Replay_Time duration (Feature_Window count of the run × Feature_Window_Span) is shorter than Minimum_State_Duration into the preceding run, or into the following run when the short run is the first run, combining adjacent runs that then share the same Sleep_Stage, and repeating until no run is shorter than Minimum_State_Duration.
10. FOR ALL SleepSessions and Target_Durations, the Feature_Windows SHALL cover the entire SleepSession from start_time to end_time without gaps or overlaps, and the Feature_Window count SHALL equal the Target_Duration divided by the Feature_Window_Span, rounded up (invariant).
11. FOR ALL Feature_Windows, each Continuous_Metric's non-missing mean SHALL lie between its minimum and maximum inclusive, each missing-sample fraction and each Movement_Intensity SHALL lie between 0.0 and 1.0 inclusive, and the Sleep_Stage fractions SHALL sum to 1.0 within a tolerance of 0.000001 (invariant).
12. FOR ALL SleepSessions and Target_Durations, the Coarse_State sequence length SHALL equal the Feature_Window count, and every run of consecutive equal Coarse_States SHALL span at least Minimum_State_Duration of Replay_Time (invariant).

### Requirement 13: Night Event Detection

**User Story:** As a listener, I want a few notable moments of the night detected and marked, so that I can connect what I hear with what happened.

#### Acceptance Criteria

1. WHEN the Event_Detector processes a SleepSession that has a sleep onset time, THE Event_Detector SHALL create exactly one sleep onset Night_Event with Night_Time equal to the sleep onset time.
2. WHEN a Stage_Run with Sleep_Stage awake starts after the sleep onset time, ends at or before the final wake time, and lasts at least Wake_Event_Min_Duration, THE Event_Detector SHALL create one wake Night_Event, described as "awake", with Night_Time equal to the start of the Stage_Run.
3. WHEN the Event_Detector processes a SleepSession that has a final wake time, THE Event_Detector SHALL create exactly one awakening Night_Event with Night_Time equal to the final wake time.
4. WHEN a Stage_Run with Sleep_Stage light, deep, or rem starts after the sleep onset time and lasts at least Major_Transition_Min_Duration, THE Event_Detector SHALL create one stage transition Night_Event, described as "light sleep", "deep sleep", or "REM" respectively, with Night_Time equal to the start of the Stage_Run.
5. WHEN a Movement_Run for Movement_Burst_Threshold begins, THE Event_Detector SHALL create one movement Night_Event with Night_Time equal to the start of the first Feature_Window of the Movement_Run.
6. WHEN a Movement_Run for Restless_Threshold lasts at least Restless_Min_Duration, THE Event_Detector SHALL create one restless period Night_Event with Night_Time equal to the start of the first Feature_Window of the Movement_Run.
7. WHEN the smoothed value of temperature, humidity, or pressure at an Aligned_Timeline sample differs from the smoothed value at one or more earlier samples within the metric's environmental change time span (30 min for temperature and humidity, 3 h for pressure) by at least the metric's environmental change threshold in the Canonical_Unit, THE Event_Detector SHALL create one environmental Night_Event at that sample, described as "<metric> rising" when the difference with the greatest absolute value is positive and "<metric> falling" when it is negative.
8. THE Event_Detector SHALL evaluate environmental change only between pairs of samples that are both not marked missing in the Missing_Data_Mask and that both lie at or after the Night_Time of the most recently created environmental Night_Event of the same metric, so that a metric with fewer than two non-missing samples produces no environmental Night_Events.
9. IF a Night_Event has a Night_Time no more than the same-type event merge window after a retained Night_Event of the same type, THEN THE Event_Detector SHALL discard the later Night_Event and set the magnitude of the retained Night_Event to the greater of the two magnitudes, processing Night_Events in ascending Night_Time order.
10. IF more than Max_Event_Count Night_Events remain after merging, THEN THE Event_Detector SHALL retain the sleep onset and awakening Night_Events where present, fill the remaining places up to Max_Event_Count with the other Night_Events in descending magnitude order, breaking magnitude ties by earlier Night_Time, and discard the rest.
11. THE Event_Detector SHALL assign every Night_Event a type corresponding to exactly one Event_Vocabulary description, a Night_Time within the SleepSession, a Replay_Time equal to the Night_Time mapped by the linear Night_Time to Replay_Time mapping of Requirement 11, a magnitude between 0.0 and 1.0, and a label formed by the Night_Time in 24-hour HH:MM format in the Display_Timezone, truncated to the minute, followed by a space and the Event_Vocabulary description (for example "02:17 restless period", "03:42 temperature rising", "05:10 REM", "06:41 awakening").
12. THE Event_Detector SHALL order Night_Events by ascending Night_Time, breaking ties by the order of descriptions in the Event_Vocabulary.
13. FOR ALL detected Night_Events, the Replay_Time SHALL be greater than or equal to 0 and less than or equal to the Target_Duration (invariant).
14. THE Event_Detector SHALL compute Night_Event magnitude as 1.0 for sleep onset and awakening; min(1.0, Stage_Run duration ÷ (2 × Wake_Event_Min_Duration)) for wake; min(1.0, Stage_Run duration ÷ (2 × Major_Transition_Min_Duration)) for stage transition; the maximum Movement_Intensity of the Movement_Run for movement; the mean Movement_Intensity of the Movement_Run for restless period; and min(1.0, greatest absolute difference ÷ (2 × environmental change threshold)) for environmental Night_Events.
15. IF the SleepSession has no Stage_Segment whose Sleep_Stage is neither awake nor unknown, THEN THE Event_Detector SHALL create no sleep onset, wake, awakening, or stage transition Night_Events and SHALL create movement, restless period, and environmental Night_Events from the available data.
16. IF no detection condition of this requirement is met, THEN THE Event_Detector SHALL produce an empty Night_Event list, and THE Backend SHALL continue Replay generation without a User_Error.

### Requirement 14: Sample Data

**User Story:** As a new user or contributor, I want a realistic synthetic night included, so that the project works immediately without personal data.

#### Acceptance Criteria

1. THE Repository SHALL include a Sample_Dataset in `sample_data/` made of Fitbit sleep, heart rate, HRV details, and steps files named according to the file name patterns in the Input Format Assumptions, plus one SensorPush_CSV whose header row states the temperature, humidity, and pressure units, all in the formats listed in the Input Format Assumptions.
2. THE Sample_Dataset SHALL contain exactly one Fitbit sleep log entry with type stages and isMainSleep true, whose startTime and endTime are exactly 8 hours apart in the Sample_Timezone, on a night with no daylight saving time transition in the Sample_Timezone.
3. THE Sample_Dataset SHALL contain 4 to 6 sleep cycles, counted as rem runs of at least 5 minutes whose start times are at least 60 minutes apart, with more total deep duration in the first 4 hours of the sleep log than in the last 4 hours and more total rem duration in the last 4 hours than in the first 4 hours.
4. THE Sample_Dataset SHALL contain integer heart rate values at 5-second intervals across the sleep log period, each between 45 and 80 bpm inclusive, with a mean over deep Stage_Segments at least 5 bpm below the mean over awake Stage_Segments, at least one gap without samples lasting between 10 and 30 minutes, and heart rate coverage of at least 90% of the sleep log period.
5. THE Sample_Dataset SHALL contain rmssd values at 5-minute intervals across the sleep log period, each between 20 and 90 ms inclusive.
6. THE Sample_Dataset SHALL contain a step count for every minute of the sleep log period, including at least three movement bursts whose start times are more than 15 minutes apart and at least one restless period, where a movement burst makes Movement_Intensity reach Movement_Burst_Threshold and a restless period keeps Movement_Intensity at or above Restless_Threshold for at least Restless_Min_Duration, as computed by the Feature_Extractor for a Target_Duration of 3 minutes.
7. THE Sample_Dataset SHALL contain SensorPush readings at 1-minute intervals across the sleep log period with humidity between 35 and 60 percent inclusive, at least one span of 30 minutes or less in which smoothed temperature changes by at least 1.0 °C, at least one span of 3 hours or less in which smoothed pressure changes by at least 1.0 hPa, and at least one gap without readings lasting between 20 and 60 minutes that overlaps neither of those spans.
8. WHEN run twice with the same Random_Seed on Windows or Linux, THE Sample_Data_Generator SHALL produce byte-identical Sample_Dataset files, and its output for the Random_Seed documented for the Sample_Dataset SHALL be byte-identical to the files in `sample_data/`.
9. FOR ALL Random_Seeds, writing the night produced by the Sample_Data_Generator to Fitbit_Export and SensorPush_CSV files and importing those files with the Importers, using the Sample_Timezone as Source_Timezone, SHALL reproduce the generated TelemetryPoints with timestamps identical to the second and values within half a unit of the last decimal place written to the file (compared in the file's unit), and the generated Stage_Segments with identical start times, end times, Sleep_Stages, and Brief_Awakening flags (round-trip property).
10. THE Documentation SHALL state that the Sample_Dataset is synthetic, was produced by the Sample_Data_Generator, is unrelated to any real person, and uses the Sample_Timezone.
11. WHEN the user activates the "Use sample data" action, THE Backend SHALL import the Sample_Dataset using the Sample_Timezone as the Source_Timezone for every Sample_Dataset file whose default Source_Timezone is the Display_Timezone, whatever the host machine timezone.
12. WHEN an import of the Sample_Dataset completes, THE Backend SHALL report exactly one candidate SleepSession lasting 8 hours and an Import_Report with zero skipped files, zero skipped values and rows, and no warnings.
13. THE Sample_Dataset SHALL contain, after sleep onset and before the final wake time, at least two awake runs each lasting between 5 and 20 minutes with start times more than 15 minutes apart, and at least three Brief_Awakenings each lasting between 30 and 90 seconds.

### Requirement 15: Local Storage Foundation

**User Story:** As a user sharing personal health and home data with the application, I want everything processed and stored locally, so that my data stays on my machine.

#### Acceptance Criteria

1. THE Backend and the CLI SHALL write imported files, normalized telemetry, Replays, the Metadata_Store, log files, and every temporary file derived from imported data (including partially received uploads and extracted archive members) only within the Data_Directory, except CLI output files written to a path the user specifies on the command line.
2. THE Backend and the CLI SHALL use the path in the `SLEEP_REPLAY_DATA_DIR` environment variable as the Data_Directory when the variable is set to a non-empty value, use the default path documented in the README otherwise, and create the Data_Directory at startup if it does not exist.
3. THE Backend and the CLI SHALL restrict log output at every log level, including exception traces and request logs, to operational metadata (file names, record counts, durations, error codes, reference identifiers, warning and error descriptions) and exclude telemetry values, Stage_Segments, per-sample timestamps, and uploaded file contents.
4. IF the Data_Directory cannot be created or is not writable at startup, THEN THE Backend and the CLI SHALL exit with a non-zero status before accepting requests or reading input data and report a User_Error naming the Data_Directory path and the `SLEEP_REPLAY_DATA_DIR` override.

### Requirement 16: Structured Errors and Warnings

**User Story:** As a user, I want every error to tell me what went wrong and what to do, so that I can fix problems without reading code.

#### Acceptance Criteria

1. THE Backend SHALL include in every User_Error an error code that is identical for every occurrence of the same error condition and is listed in the Documentation troubleshooting section, a plain-language problem description that contains no stack trace, exception type name, or source code reference, the affected file name or archive member name when the error concerns a specific file, and the action the user needs to take.
2. WHEN an import contains at least one valid file and at least one invalid file (unsupported file name pattern, malformed content, missing required field, or archive member exceeding Max_Archive_Member_Size), THE Importer SHALL import the data of every valid file and list each invalid file in the Import_Report with its file name and skip reason.
3. IF an import yields no TelemetryPoint and no candidate SleepSession from any provided file, an import contains no file matching a supported Fitbit file name pattern, session discovery finds no candidate SleepSession, or the selected SleepSession has no stage data, no heart rate data, and no environmental data, THEN THE Backend SHALL stop the operation and return a User_Error rather than a warning.
4. WHEN an import, session discovery, or alignment completes despite a skipped unsupported or malformed file, a metric missing for the SleepSession, merged overlapping SleepSessions, a daylight saving time adjustment, a likely Source_Timezone mismatch, or a Continuous_Metric gap longer than the Max_Interpolation_Gap, THE Backend SHALL complete the operation and add a warning containing a plain-language description, the affected file name or metric, and the recommended user action (or a statement that no action is required) to the Import_Report for import and session discovery conditions and to the Processing_Report for alignment conditions, with one warning per affected metric for gaps stating the gap count and total gap duration.
5. IF an operation ends with a User_Error, THEN THE Backend SHALL leave the imports, SleepSessions, settings, and Replays stored before the operation unchanged and retain no partial output of the failed operation, including partial WAV files and Replay records.

### Requirement 17: Architecture and Separation of Concerns

**User Story:** As a contributor, I want ingestion, domain, processing, sonification, audio, API, and UI clearly separated, so that each part can evolve independently.

#### Acceptance Criteria

1. THE Repository SHALL organize the Backend into six separate Python packages, one each for ingestion, domain, processing, sonification, audio, and API, with the ingestion package containing one sub-package per Importer (Fitbit_Importer, SensorPush_Importer), the Frontend in a top-level directory outside all Backend packages, and the Documentation naming the package that implements each concern.
2. THE Repository SHALL satisfy the Dependency_Rules, where a package depends on another package when any of its source files imports that package through an absolute or relative import, at module level or inside a function, standard-library and third-party imports being unrestricted: (a) the domain package imports no other Backend package; (b) the ingestion package imports no processing, sonification, audio, or API package; (c) no Importer sub-package imports another Importer sub-package; (d) the processing, sonification, and audio packages import no ingestion package; (e) the ingestion, domain, processing, sonification, and audio packages import no API package.
3. THE Test_Suite SHALL include a dependency test that checks the import statements of every Backend source file against each Dependency_Rule, fails when any rule is violated, and names the violating source file and the imported package in its failure output.
4. THE Repository SHALL place the definitions of TelemetryPoint, Stage_Segment, SleepSession, Night_Event, and the Data_Source_Adapter interface in the domain package, and every Importer SHALL implement that Data_Source_Adapter interface.
5. THE Frontend SHALL access imports, SleepSessions, Mapping_Config values, and Replays only through the Backend_API, without importing Backend source files and without reading the Data_Directory or the Metadata_Store directly.

### Requirement 18: Automated Testing for the Data Pipeline

**User Story:** As a contributor, I want a comprehensive automated test suite, so that changes to processing or sound generation do not silently break the replay.

#### Acceptance Criteria

1. THE Test_Suite SHALL include at least one automated test for each of the following areas: telemetry normalization, Fitbit import, SensorPush import, sleep session discovery and selection, timestamp alignment, timezone handling (including a SleepSession spanning a daylight saving time transition), resampling and Max_Interpolation_Gap handling, sleep-stage handling, feature extraction, night compression time mapping, and event detection.
2. THE Test_Suite SHALL include a property-based test for every acceptance criterion in this spec labeled as an invariant or as a round-trip, monotonicity, order-preservation, or confluence property, each running at least 100 generated cases (at least 5 where a single case renders a Replay or generates a full night with the Sample_Data_Generator) and reporting the generated input that caused any failure.
3. THE Test_Suite SHALL run with one command documented in the README, use as input data only the Sample_Dataset, Sample_Data_Generator output, and synthetic fixture files stored in the Repository, and exit with a non-zero exit code when any test fails and a zero exit code when all tests pass.
4. WHEN run with the documented command on the Reference_Machine with all dependencies installed, THE Test_Suite SHALL complete within 5 minutes, measured from command start to process exit and excluding dependency installation and Docker image builds.
5. THE Test_Suite SHALL include Importer tests that cover at minimum: a Fitbit_Export provided as individual files and as one zip archive; Fitbit file names that differ only in letter case; stages-style and classic-style sleep logs, including levels.shortData entries; SensorPush_CSV files with comma delimiters and with semicolon delimiters, each with and without a UTF-8 byte order mark; each SensorPush header keyword and header unit listed in the Input Format Assumptions; SensorPush measurement headers without a unit; each SensorPush timestamp format listed in the Input Format Assumptions; and malformed inputs (malformed JSON, malformed CSV, a file missing a required field, a SensorPush_CSV without a recognizable timestamp column, and an unrecognized file inside a Fitbit_Export).
6. WHILE outbound network access is disabled on the host, THE Test_Suite SHALL produce the same pass/fail result for every test as it does with network access enabled.
7. THE Test_Suite SHALL write every file and Metadata_Store database it creates to temporary directories, and leave the user's configured Data_Directory and the committed Repository files (including the Sample_Dataset and the example Replay) unmodified.
