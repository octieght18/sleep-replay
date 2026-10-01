# Requirements Document

## Introduction

Sleep Replay is an open-source, local-first application that turns one night of sleep and bedroom-environment telemetry into a short ambient audio composition (30 seconds to 10 minutes, default 3 minutes). After waking, the user imports a Fitbit export and a SensorPush CSV. The application discovers the night's sleep session, aligns the datasets on a common timeline, extracts features and events, maps them to a small set of interacting musical dimensions, and renders a WAV file. The user presses Play and follows a simple timeline with a moving playhead, the current coarse sleep state, and a few event markers.

Example: 8 hours of telemetry → normalize and align → extract features and events over time windows → map to sound parameters → render ~3 minutes of new audio (160x temporal compression).

The MVP answers one question: is listening to a compressed auditory representation of my night's sleep and bedroom environment an interesting and useful experience? A working, pleasant-sounding replay takes priority over feature count. The audio experience is the product; the browser UI stays minimal. Sleep Replay is not a medical device, not a diagnostic tool, and not an alarm.

This spec, sleep-replay-app, is the third of three specs split from the Sleep Replay MVP requirements. It builds on sleep-replay-data-pipeline, which provides import, session discovery, alignment, feature extraction, and Night_Event detection, and on sleep-replay-sonification, which turns them into a WAV file with a Replay_Manifest through the CLI. It covers the Backend_API, the Import_View, Main_Screen, Playback_Timeline, and Settings_Panel, settings persistence and Replay caching, privacy and network restrictions, error display, setup and Docker, the remaining Documentation deliverables, and the Backend_API tests. Its result is the complete end-to-end user flow through a minimal browser UI, started with `docker compose up` or the documented Windows setup.

## Related Specs

Terms not defined in this document are defined in the Glossaries of sleep-replay-data-pipeline and sleep-replay-sonification. sleep-replay-data-pipeline also holds the Input Format Assumptions and the Technology and Environment Constraints.

#[[file:.kiro/specs/sleep-replay-data-pipeline/requirements.md]]
#[[file:.kiro/specs/sleep-replay-sonification/requirements.md]]

## Scope

### In Scope (this spec)

- End-to-end flow with local files: start application → import Fitbit export → import SensorPush data → discover/select sleep session → align by timestamp → generate replay → press Play → view timeline synchronized with playback → regenerate the same night with different mapping settings.
- Local-first, single-user MVP scope and the non-medical statement (Requirement 1).
- Import_View (Requirement 2).
- Main_Screen and Replay generation through the Backend_API (Requirement 3).
- Playback and Playback_Timeline synchronization (Requirement 4).
- Settings_Panel, settings persistence, regeneration, and Replay caching (Requirement 5).
- First start with the Sample_Dataset (Requirement 6).
- Privacy and network restrictions (Requirement 7).
- Error reporting in the Backend_API and the Frontend (Requirement 8).
- Setup and runtime environment, including Docker (Requirement 9).
- Automated tests for the Backend_API, settings persistence, and Replay caching (Requirement 10).
- Documentation and deliverables (Requirement 11).

### Out of Scope (this spec)

- Specified in sleep-replay-data-pipeline: the domain model, Fitbit and SensorPush import, the Data_Source_Adapter interface, sleep session discovery and selection, timezone handling, alignment and resampling, the night compression time mapping and Target_Duration validation, feature extraction and Coarse_States, Night_Event detection, the Sample_Dataset and Sample_Data_Generator, the Data_Directory and local persistence foundation, log content restrictions, User_Error structure and report warnings, and the architecture and Dependency_Rules.
- Specified in sleep-replay-sonification: Mapping_Config parsing, validation, and printing; Soundscape_Presets; the sound mappings; parameter smoothing; audio rendering and output; determinism of Replays; the Replay_Manifest and Manifest_Serializer; graceful degradation; the CLI; the example Replay WAV file.
- Where a criterion of this spec relies on import, processing, sonification, or Replay generation behavior, this spec specifies the Backend_API and Frontend behavior; the underlying behavior is specified in the spec listed above.

### Non-Goals (MVP)

The product-wide Non-Goals are listed in sleep-replay-data-pipeline and apply unchanged to this spec.

## Default Parameter Values

The parameters referenced by this spec (for example Max_Upload_Size, Display_Units, and the default Random_Seed) are defined in the Default Parameter Values of sleep-replay-data-pipeline and sleep-replay-sonification; references in this spec to the Default Parameter Values table mean both tables together.

## Glossary

All terms used by this spec are defined in the Glossaries of sleep-replay-data-pipeline and sleep-replay-sonification.

## Requirements

### Requirement 1: Local-First MVP Scope

**User Story:** As a person curious about my sleep, I want a single-user application that runs entirely on my computer from local files, so that I can find out whether listening to a replay of my night is interesting and useful.

#### Acceptance Criteria

1. THE Sleep_Replay SHALL run on the user's local machine as a single-user application with no sign-in, account-creation, or user-selection step.
2. THE Sleep_Replay SHALL support the complete user flow through the Frontend, without requiring the CLI or manual file edits. The flow is: starting the application, importing a Fitbit_Export, importing a SensorPush_CSV, selecting a SleepSession from the candidates discovered by the Session_Detector, aligning the data onto the Aligned_Timeline, generating a Replay, playing the Replay with a synchronized Playback_Timeline, and regenerating a Replay of the same SleepSession after changing Mapping_Config values in the Settings_Panel, without re-importing any file.
3. THE Sleep_Replay SHALL accept local files as the only data input and SHALL request no user account, cloud service access, or device API credentials at any step of the user flow in criterion 2.
4. THE Sleep_Replay SHALL label Coarse_States and Night_Events only with Sleep_Stage names, Soundscape_Preset names, or Event_Vocabulary entries. The Frontend, CLI output, and Replay_Manifest SHALL contain no sleep quality score, health rating, diagnosis, or medical condition name.
5. WHILE the Import_View or the Main_Screen is displayed, THE Frontend SHALL show a statement that Sleep Replay is an aesthetic auditory replay, is not a medical device, diagnostic tool, or alarm, and provides no diagnosis or health advice. The statement SHALL be visible in a 1280 × 720 pixel browser viewport without scrolling or opening a dialog or menu.
6. WHILE the local machine has no external network connection, THE Sleep_Replay SHALL complete the user flow in criterion 2 once installation is finished (Docker images built, or the Python virtual environment and frontend dependencies installed), with all Frontend assets served from the local machine.
7. THE Sleep_Replay SHALL store imported files, TelemetryPoints, SleepSessions, settings, and Replays only in the Data_Directory on the local machine, and SHALL send none of them to any destination outside the local machine.
8. THE Backend_API and the Frontend SHALL accept connections only from the local machine and refuse connections from other hosts, in both the Docker setup and the non-Docker setup.

### Requirement 2: Import View

**User Story:** As a user, I want a simple screen to import my files or the sample data, so that I can get to a replay quickly.

#### Acceptance Criteria

1. THE Import_View SHALL provide a file selector for Fitbit_Export data that accepts either one or more files with a `.json` or `.csv` extension or exactly one file with a `.zip` extension, with extensions matched case-insensitively.
2. THE Import_View SHALL provide a file selector for SensorPush_CSV data that accepts one or more files with a `.csv` extension, with extensions matched case-insensitively.
3. WHEN the user activates the "Use sample data" action, THE Import_View SHALL start one import of all Sample_Dataset files (Fitbit_Export and SensorPush_CSV) without requiring any file selection, using the default Source_Timezone of each file type.
4. THE Import_View SHALL provide, for each file type listed in the Input Format Assumptions, an optional Source_Timezone override selectable from IANA timezone identifiers and pre-set to that file type's default Source_Timezone (UTC or the current Display_Timezone), applying only to timestamps without an explicit offset.
5. WHILE an import is in progress, THE Import_View SHALL display a progress indicator and disable both file selectors, the Source_Timezone overrides, the Import action, and the "Use sample data" action.
6. WHEN an import completes, THE Import_View SHALL display the Import_Report, including accepted files, skipped files with reasons, skipped value and row counts, per-metric counts and time coverage (start and end in the Display_Timezone), and warnings.
7. IF a selected file exceeds Max_Upload_Size, THEN THE Import_View SHALL, without uploading any file, display a User_Error naming the file and recommending import through the CLI or selecting only the Fitbit files listed in the Input Format Assumptions.
8. WHEN the user activates the Import action while at least one file is selected in either file selector, THE Import_View SHALL start one import containing all selected Fitbit_Export and SensorPush_CSV files and the current Source_Timezone values.
9. IF the Fitbit_Export selection contains a `.zip` file together with any other file, or either file selector contains a file with an extension that selector does not accept, THEN THE Import_View SHALL, without uploading any file, display a User_Error naming the rejected files and stating the accepted file combinations.
10. IF an import fails because the Backend_API returns a User_Error or cannot be reached, THEN THE Import_View SHALL hide the progress indicator, display the User_Error (for an unreachable Backend_API, a User_Error indicating that the Backend is not running), keep the file selections and Source_Timezone values, and re-enable the controls disabled during the import.

### Requirement 3: Main Screen and Replay Generation

**User Story:** As a user, I want a minimal main screen with Generate and Play, so that the audio experience stays the focus.

#### Acceptance Criteria

1. WHILE a SleepSession is selected, THE Main_Screen SHALL display the title "Sleep Replay", the session label, the SleepSession start_time and end_time as 12-hour clock times in the form "h:mm AM/PM → h:mm AM/PM" (for example "11:42 PM → 7:05 AM") in the Display_Timezone, a Generate Replay button, a Play button, and the Playback_Timeline labeled "00:00" at the start and the Target_Duration in mm:ss format (for example "03:00") at the end.
2. WHEN the Main_Screen displays a selected SleepSession whose end_time falls on the current date in the Display_Timezone, THE Main_Screen SHALL use "Last night" as the session label.
3. WHEN the Main_Screen displays a selected SleepSession whose end_time falls on a date other than the current date in the Display_Timezone, THE Main_Screen SHALL use "Night of" followed by the SleepSession start date in the Display_Timezone in the form "MMM D, YYYY" (for example "Night of Mar 3, 2025") as the session label.
4. WHILE the Session_Detector lists more than one candidate SleepSession, THE Main_Screen SHALL display a session selector that lists every candidate SleepSession in the Session_Detector order with its start date, time range, and duration, with the selected SleepSession preselected.
5. WHEN the user activates Generate Replay, THE Frontend SHALL pause any ongoing playback and request generation of a Replay for the selected SleepSession with the current Mapping_Config, including the Target_Duration and Random_Seed set in the Settings_Panel.
6. WHILE a Replay is being generated, THE Main_Screen SHALL display a progress indicator, starting no later than 1 s after the user activates Generate Replay, and disable the Generate Replay button, the Play button, and the session selector.
7. WHEN Replay generation completes, THE Main_Screen SHALL hide the progress indicator, load the new Replay with the playhead at 00:00 and the Night_Event markers and Playback_Timeline end label taken from its Replay_Manifest, and enable the Generate Replay button, the Play button, and the session selector without starting playback.
8. IF Replay generation fails or the Backend_API cannot be reached, THEN THE Main_Screen SHALL hide the progress indicator, display the User_Error description and required action (or a message indicating that the Backend is not reachable and needs to be started), re-enable the Generate Replay button and the session selector, and keep any previously loaded Replay loaded and playable.
9. WHEN a Replay is loaded whose Replay_Manifest lists warnings or unavailable metrics, THE Main_Screen SHALL display every listed warning and the name of every unavailable metric until the Replay is unloaded or another Replay is loaded.
10. IF the Backend receives a generation request while another generation is in progress, THEN THE Backend SHALL reject the request with a User_Error stating that a generation is running and that the user needs to wait for it to finish, and continue the running generation unchanged.
11. THE Main_Screen SHALL limit the displayed content to the elements in criterion 1, the Pause button, the session selector, the progress indicator, the Coarse_State, the Night_Time at the playhead, the temperature, humidity, and pressure values at the playhead, Night_Event markers and labels, warnings and unavailable metrics, User_Error descriptions and required actions, the manual time range inputs, the non-medical statement, and navigation to the Import_View and Settings_Panel.
12. WHILE no Replay is loaded for the selected SleepSession, THE Main_Screen SHALL disable the Play button, show the playhead at 00:00, display no Night_Event markers, Coarse_State, or environmental values, and label the end of the Playback_Timeline with the Target_Duration set in the Settings_Panel.
13. WHEN the user chooses a different candidate SleepSession in the session selector, THE Main_Screen SHALL stop playback, unload the loaded Replay, and display the session label and time range of the chosen SleepSession.
14. IF the Session_Detector returns a User_Error indicating that no candidate SleepSession exists, THEN THE Main_Screen SHALL display the User_Error description and required action, provide start time and end time inputs for a manual time range in the Display_Timezone, and disable the Generate Replay button until a SleepSession is selected.

### Requirement 4: Playback and Timeline Synchronization

**User Story:** As a listener, I want a moving playhead with the current state and a few markers, so that I know which part of the night I am hearing.

#### Acceptance Criteria

1. WHEN the user activates Play, THE Frontend SHALL start audio playback from the playhead position within 500 ms and replace the Play button with a Pause button.
2. WHEN the user activates Pause, THE Frontend SHALL pause playback within 200 ms, keep the playhead at the paused position, and replace the Pause button with the Play button.
3. WHILE a Replay is playing, THE Playback_Timeline SHALL update the playhead at least 10 times per second at a position within 100 ms of the audio playback position.
4. WHILE a Replay is loaded, THE Main_Screen SHALL display the label of the Replay_Manifest Coarse_State segment that contains the playhead position (segment start inclusive, segment end exclusive, final segment at the Target_Duration), mapping awake to "Awake", light to "Light Sleep", deep to "Deep Sleep", rem to "REM", asleep to "Asleep", restless to "Restless", and unknown to "Unknown".
5. WHILE a Replay is loaded, THE Main_Screen SHALL display the Night_Time equal to the SleepSession start_time plus the playhead Replay_Time multiplied by the Compression_Ratio, in the Display_Timezone, in 12-hour "h:mm AM" or "h:mm PM" format truncated to the minute.
6. WHILE a Replay is loaded, THE Main_Screen SHALL display each of temperature, humidity, and pressure that the Replay_Manifest lists as available, omit each one listed as unavailable, and show for each displayed metric the environmental value of the Feature_Window that contains the playhead position, in the Display_Units currently selected (temperature to 0.1 °F or 0.1 °C, humidity to 1 percent, pressure to 0.01 inHg or 0.1 hPa), or a placeholder indicating no data where that Feature_Window value is marked missing.
7. WHILE a Replay is loaded, THE Playback_Timeline SHALL display one keyboard-focusable marker per Night_Event in the Replay_Manifest at the fraction Replay_Time ÷ Target_Duration of the Playback_Timeline width (within 0.5% of that width), with keyboard focus order following Night_Time order and the Night_Event label as the marker's accessible name.
8. WHEN a Night_Event marker receives pointer hover or keyboard focus, THE Playback_Timeline SHALL display the Night_Event label next to the marker until both pointer hover and keyboard focus have left the marker.
9. WHEN the user clicks or taps a point on the Playback_Timeline, THE Frontend SHALL move the playback position and the playhead to the Replay_Time equal to the point's fractional horizontal position multiplied by the Target_Duration, keeping the current playing or paused state.
10. WHEN playback reaches the end of the Replay, THE Frontend SHALL stop playback, keep the playhead at the Target_Duration, and replace the Pause button with the Play button.
11. WHEN the user activates Play while the playhead is at the Target_Duration, THE Frontend SHALL restart playback from 00:00.
12. WHEN the playhead position changes through playback, seeking, or loading a Replay, THE Main_Screen SHALL update the displayed Coarse_State, Night_Time, and environmental values within 200 ms, deriving these values and the Night_Event markers only from the Replay_Manifest of the loaded Replay.
13. WHEN the Playback_Timeline has keyboard focus and the user presses Left Arrow, Right Arrow, Home, or End, THE Frontend SHALL move the playback position and the playhead 5 s of Replay_Time backward, 5 s forward, to 00:00, or to the Target_Duration respectively, clamped to the range 00:00 to the Target_Duration, keeping the current playing or paused state.
14. WHEN a new Replay is loaded on the Main_Screen, THE Frontend SHALL stop playback of the previously loaded Replay, set the playhead to 00:00, display the Play button, and replace the displayed Coarse_State, Night_Time, environmental values, and Night_Event markers with those derived from the new Replay_Manifest.
15. IF the Replay audio fails to load, fails to decode, or cannot start playback, THEN THE Frontend SHALL display an error message indicating that the Replay audio could not be played along with the required user action, display the Play button, and keep the playhead at its current position.

### Requirement 5: Settings Panel and Regeneration

**User Story:** As a user, I want a basic settings panel to adjust mappings and regenerate, so that I can hear the same night in different ways.

#### Acceptance Criteria

1. THE Settings_Panel SHALL provide the following controls, each with a visible text label and operable by keyboard: a Target_Duration selector offering 30 s, 2 min, 3 min, 5 min, and 10 min; for each Mapping_Config metric key, a target selector offering the Sound_Parameter mapping targets and none, and a Sensitivity control from 0.0 to 1.0 in steps of 0.05; a Random_Seed field accepting integers from 0 to 4,294,967,295; and a Display_Units selector offering imperial and metric.
2. WHEN the user activates Generate Replay, THE Backend SHALL generate a Replay for the SleepSession currently selected on the Main_Screen, using a Mapping_Config built from the current valid Settings_Panel values for Target_Duration, per-metric targets, per-metric Sensitivities, and Random_Seed, plus the default smoothing window and Hysteresis_Threshold values.
3. WHEN the user changes a Settings_Panel control to a valid value, THE Backend SHALL persist the complete application-wide settings (Target_Duration, per-metric targets and Sensitivities, Random_Seed, and Display_Units) in the Metadata_Store within 1 s and without generating a Replay.
4. WHEN the Frontend starts and settings are persisted in the Metadata_Store, THE Settings_Panel SHALL display the persisted settings, including after the Sleep_Replay has been restarted.
5. WHEN the user activates the restore-defaults action, THE Settings_Panel SHALL set every per-metric target and Sensitivity to its Default_Mapping value and leave Target_Duration, Random_Seed, and Display_Units unchanged.
6. IF the user enters an empty value, a Sensitivity outside 0.0 to 1.0 or not a multiple of 0.05, or a Random_Seed that is not an integer from 0 to 4,294,967,295, THEN THE Settings_Panel SHALL display a validation message adjacent to the control stating the allowed values, keep the last valid value as the value used for persistence and generation, and remove the message once the control holds a valid value.
7. WHEN generation is requested and the Data_Directory holds a Replay WAV file and Replay_Manifest whose Input_Fingerprint, software version, and Mapping_Config (after applying defaults, including Target_Duration and Random_Seed) are identical to those of the request, THE Backend SHALL return the stored Replay within 2 s on the Reference_Machine without rendering audio again.
8. WHEN the user changes Display_Units, THE Main_Screen SHALL show temperature in °F and pressure in inHg for imperial, or temperature in °C and pressure in hPa for metric, within 1 s and without generating a new Replay.
9. IF the Backend receives settings containing a value outside the options and ranges in criterion 1, or a Mapping_Config that fails Mapping_Config_Parser validation, THEN THE Backend SHALL return a User_Error naming the offending setting and its allowed values and leave the persisted settings unchanged.
10. WHEN the Frontend starts and no settings are persisted in the Metadata_Store, THE Settings_Panel SHALL display the Default_Mapping, a Target_Duration of 3 min, the default Random_Seed, and imperial Display_Units.

### Requirement 6: First Start with Sample Data

**User Story:** As a new user, I want the sample data offered when I first start the application, so that the project works immediately without personal data.

#### Acceptance Criteria

1. WHEN the Frontend starts and the Metadata_Store contains no imported data, THE Frontend SHALL display the Import_View with the "Use sample data" action.

### Requirement 7: Privacy and Network Restrictions

**User Story:** As a user sharing personal health and home data with the application, I want everything processed and stored locally, so that my data stays on my machine.

#### Acceptance Criteria

1. THE Sleep_Replay SHALL process all data on the local machine and, after installation, open network connections only to loopback addresses, with no analytics, usage telemetry, update checks, crash reports, or asset requests sent to other hosts by the application or its bundled frameworks.
2. THE Frontend SHALL load all scripts, styles, fonts, images, and icons from local servers of the Sleep_Replay and reference no external origin such as a CDN or hosted font service.
3. THE Sleep_Replay SHALL, by default, accept connections to the Backend_API and to the server hosting the Frontend only through the host machine's loopback interface, both without Docker and under Docker (Requirement 9).
4. WHERE the default Data_Directory lies inside the Repository, THE Repository SHALL exclude the default Data_Directory from version control and from the Docker build context through ignore rules while keeping the Sample_Dataset files and the example Replay WAV file tracked.
5. IF the user configures the Backend_API or the server hosting the Frontend to listen on a non-loopback interface, THEN THE Sleep_Replay SHALL print a startup warning in its console output stating that the Backend_API has no authentication and that imported data becomes reachable from other devices on the network.
6. IF the Backend_API receives a request sent from a browser page whose origin is not the Frontend's origin, THEN THE Backend_API SHALL reject the request with a User_Error, without performing the requested operation and without returning SleepSession, telemetry, or Replay data.

### Requirement 8: Error Reporting in the API and Frontend

**User Story:** As a user, I want every error to tell me what went wrong and what to do, so that I can fix problems without reading code.

#### Acceptance Criteria

1. WHEN a Backend_API request fails, THE Backend_API SHALL return the User_Error as structured JSON with a 4xx status code when the cause lies in user-provided input or request state (files, parameters, Mapping_Config, manual time ranges, or a concurrent generation request) and a 5xx status code when the cause is an unexpected internal error.
2. IF an unexpected internal error occurs while the Backend handles a Backend_API request or CLI command, THEN THE Backend SHALL write a log entry containing the error details and a reference identifier unique to the occurrence, excluding telemetry values and per-sample timestamps, and return a User_Error containing the same reference identifier, a description stating that an internal error occurred, and the action to retry the operation and report the reference identifier if the error recurs.
3. THE Frontend SHALL display the description and required action of each User_Error.
4. IF a Frontend request to the Backend_API fails because the Backend is not reachable, THEN THE Frontend SHALL display a message indicating that the Backend is not reachable together with the action to start the Backend using the documented command, and keep the previously displayed content unchanged.

### Requirement 9: Setup and Runtime Environment

**User Story:** As a user on Windows, I want to run the application with Docker or with a simple local setup, so that I can try it without complex installation.

#### Acceptance Criteria

1. WHEN the user runs `docker compose up` from the root of a fresh Repository clone with Docker and Docker Compose installed and running, THE Sleep_Replay SHALL build and start the Backend and the Frontend and serve the Frontend at the localhost URL documented in the README, without any other setup step (no manual file creation, environment variable configuration, or separate build command).
2. WHILE running under Docker, THE Sleep_Replay SHALL publish only the Frontend and Backend_API ports and bind each published port to the host loopback interface only, so that connection attempts to those ports from other machines on the network fail.
3. WHEN the user runs the non-Docker installation and start commands documented in the README in Windows PowerShell on Windows 11, THE Sleep_Replay SHALL start the Backend and the Frontend and serve the Frontend at the localhost URL documented in the README, installing Python packages only into a Python virtual environment, without administrator privileges, and without software other than Python 3.11 or later and, only where the Frontend requires a build step, the Node.js LTS major version stated in the README.
4. WHEN the Sleep_Replay starts with an empty Data_Directory in either the Docker or the non-Docker setup, THE Frontend SHALL offer the "Use sample data" action, which imports the Sample_Dataset included in the Repository without requiring the user to run the Sample_Data_Generator, copy files, or download data.
5. THE Repository SHALL pin every direct and transitive Python and Frontend package dependency and every Docker base image to an exact version, with no version ranges, wildcards, or floating tags such as `latest`.
6. WHEN the user runs the documented start command on the Reference_Machine with an empty Data_Directory after installation has completed (dependencies installed for the non-Docker setup; container images built and Docker running for the Docker setup), THE Backend_API SHALL return a successful response to a request within 15 s of the start command.
7. WHILE running under Docker, THE Sleep_Replay SHALL keep the Data_Directory in a location, documented in the README, that persists when containers are removed, so that imports, settings, and Replays remain available after `docker compose down` followed by `docker compose up`.
8. IF the documented non-Docker installation or start command runs with a Python version earlier than 3.11, THEN THE Sleep_Replay SHALL stop before the Backend accepts requests and display an error message indicating the detected Python version and that Python 3.11 or later is required.
9. IF a port required by the Backend or the Frontend is already in use when the Sleep_Replay starts, THEN THE Sleep_Replay SHALL report an error identifying the port in use and leave the affected Backend or Frontend unstarted rather than serving it on a different port.

### Requirement 10: Automated Testing for the API and End-to-End Flow

**User Story:** As a contributor, I want a comprehensive automated test suite, so that changes to processing or sound generation do not silently break the replay.

#### Acceptance Criteria

1. THE Test_Suite SHALL include at least one automated test for each of the following areas: the Backend_API, settings persistence, and Replay caching.
2. THE Test_Suite SHALL include at least one valid-request test that verifies the response content for each Backend_API endpoint the Frontend calls and, for each such endpoint that accepts request parameters or a request body, at least one invalid-request test that verifies a User_Error with the structure and status code class defined in Requirement 8 and sleep-replay-data-pipeline Requirement 16.
3. THE Test_Suite SHALL run with one command documented in the README, use as input data only the Sample_Dataset, Sample_Data_Generator output, and synthetic fixture files stored in the Repository, and exit with a non-zero exit code when any test fails and a zero exit code when all tests pass.
4. WHEN run with the documented command on the Reference_Machine with all dependencies installed, THE Test_Suite SHALL complete within 5 minutes, measured from command start to process exit and excluding dependency installation and Docker image builds.
5. WHILE outbound network access is disabled on the host, THE Test_Suite SHALL produce the same pass/fail result for every test as it does with network access enabled.
6. THE Test_Suite SHALL write every file and Metadata_Store database it creates to temporary directories, and leave the user's configured Data_Directory and the committed Repository files (including the Sample_Dataset and the example Replay) unmodified.

### Requirement 11: Documentation and Deliverables

**User Story:** As a new user or contributor, I want clear documentation and examples, so that I can set up, import my data, and extend the project.

#### Acceptance Criteria

1. THE Repository SHALL include all source code of the Backend, Frontend, and CLI needed to run every step of the end-to-end flow listed under In Scope, so that nothing outside the Repository is needed beyond the dependencies installed by the documented setup commands.
2. THE README SHALL include the prerequisites with minimum versions (Docker with Docker Compose; Python 3.11 or later; Node.js LTS where the Frontend requires a build step), the exact `docker compose up` commands, the exact non-Docker Windows PowerShell commands, the localhost URL where the Frontend is served, the steps to generate and play a Replay from the Sample_Dataset through the Frontend and through the CLI, and the single command that runs the Test_Suite, with every command running unmodified from the Repository root.
3. THE Documentation SHALL include numbered step-by-step instructions for exporting Fitbit data through Google Takeout; exporting a SensorPush_CSV from the SensorPush app or web dashboard for a time range that covers the night; importing each through the Import_View and through the CLI; overriding the Source_Timezone at import; and staying within Max_Upload_Size and Max_Archive_Member_Size by selecting only the Fitbit files listed in the Input Format Assumptions or importing through the CLI.
4. THE Documentation SHALL include `docs/data-formats.md`, consistent with the Input Format Assumptions as confirmed in the design, listing for each supported file type the file name pattern, the fields or header keywords used, the recognized units, the unit inference rules, the accepted timestamp formats, and the default Source_Timezone, plus the Stage_Name_Mapping and the unsupported or ignored inputs (day-first dates, ignored files, ignored columns).
5. THE Documentation SHALL include an architecture document covering the components (Importers, Session_Detector, Aligner, Feature_Extractor, Event_Detector, Sonification_Engine, Audio_Renderer, Manifest_Serializer, Metadata_Store, Backend_API, Frontend, and CLI); the data flow from import to WAV output and the domain model (TelemetryPoint, Stage_Segment, SleepSession, Aligned_Timeline, Night_Event, Replay_Manifest); the Data_Source_Adapter extension point, including the steps a contributor follows to add a new Importer, how a SensorPush cloud API adapter would be added, and where its credentials would be configured outside version control; and the future sources and output targets listed under Non-Goals.
6. THE Repository SHALL include an example Mapping_Config file in YAML that the Mapping_Config_Parser accepts without errors, that sets the target and Sensitivity of every Mapping_Config metric key to the Default_Mapping, sets target_duration to 3 min and random_seed to the Default Random_Seed, sets every smoothing window and Hysteresis_Threshold that has a default in the Default Parameter Values table, and carries a comment on each metric key stating the allowed targets, the Sensitivity range 0.0 to 1.0, and the audible effect of raising the Sensitivity.
7. THE Documentation SHALL describe, for each Mapping_Config metric key, the default target and Sensitivity, the direction in which rising metric values move the target Sound_Parameter, and the Neutral_Value behavior; how contributions combine when two metrics share one Sound_Parameter; for each Sound_Parameter, the Sound_Layer and audible property it controls; the Soundscape_Preset applied for each Coarse_State; and the final value of every parameter in the Default Parameter Values table, which SHALL equal the values the Backend uses when the Mapping_Config does not override them.
8. THE Documentation SHALL state the default Data_Directory path for the Docker setup and for the non-Docker Windows setup, the `SLEEP_REPLAY_DATA_DIR` override, the location of imported files, normalized telemetry, Replays, and the Metadata_Store within the Data_Directory, and the commands that delete all local data including Docker volumes, after which the next start of the Sleep_Replay SHALL show the no-imported-data state that offers the Sample_Dataset.
9. THE Documentation SHALL state that the Backend_API has no authentication, that the Backend_API and the Docker port mappings bind to the loopback interface by default, and that binding to a non-loopback interface exposes imported data and Replays to other devices on the network without access control.
10. THE README SHALL state that Sleep Replay is not a medical device and not an alarm, provides no diagnosis or health advice, and that the sounds carry no medical meaning.
11. THE Documentation SHALL include a troubleshooting section listing every User_Error code the Backend can return with its cause and the required user action, and describing the user action for each warning condition listed in sleep-replay-data-pipeline Requirement 16 criterion 4.
12. THE Repository SHALL include a license file at the Repository root containing the full text of one OSI-approved open-source license.
13. THE Documentation SHALL list every CLI command with its arguments, options, default option values, exit code behavior, and at least one example invocation.
14. THE Test_Suite SHALL include a test that fails when a User_Error code defined in the Backend is missing from the troubleshooting section of the Documentation.
