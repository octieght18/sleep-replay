# Architecture and adapter extension

The browser is static HTML/CSS/JavaScript. Python's static server serves it on port 8734 and streams `/api/` requests to FastAPI on port 8735. Both bind loopback by default. `python -m backend.api.run` checks the Python version and reserves both ports before starting both servers. Separate entry points support two terminals or containers. There is no frontend build step.

The application uses one Pipeline, SettingsService and ReplayCache for its lifetime. A lock serializes changes to the selected/imported state; a separate nonblocking generation lock rejects overlapping generation requests. Settings save one complete SQLite record. The replay cache matches normalized input fingerprints, effective mapping configuration (including defaults and seed) and renderer version. Both artifact files must exist and the manifest must validate. A changed display timezone also requires a new manifest.

## Import to audio

1. Uploads stream into temporary files inside the data directory; extensions, combinations, limits and timezone overrides are validated before importing.
2. The registry routes each source to its `Data_Source_Adapter`. Fitbit discovers sleep sessions and physiology; SensorPush provides room telemetry. The validation gate checks timestamps, metrics, values and units. Local normalized telemetry and import metadata are saved transactionally.
3. Session detection removes duplicates, merges overlaps and preserves explicit selection. A manual range can be used with telemetry lacking sleep logs.
4. Alignment converts units, selects sensors, removes duplicate samples and interpolates only bounded gaps. Compression establishes night time to replay time; feature extraction summarizes 0.5-second replay windows. State processing and event detection produce coarse states and at most twelve merged events.
5. Sonification maps normalized features through sensitivity, hysteresis, missing-data glides, contribution combination and rate limits into an immutable render plan. Stage presets select sound character. The deterministic NumPy renderer generates four ambient voices and soft event motifs, then validates loudness and writes 44.1 kHz stereo 16-bit WAV.
6. WAV and manifest are installed atomically with SQLite metadata. The manifest contains session boundaries, states, events, windowed environmental values, availability and configuration; it excludes raw telemetry and generation timestamps. The browser derives every playback label from it and seeks the WAV using byte ranges.

Domain models include `TelemetryPoint`, `SleepSession`, `Stage_Segment`, `Aligned_Timeline`, `Feature_Series`, `Night_Event`, `Mapping_Config`, `Render_Plan` and `Replay_Manifest`. Import and processing reports contain counts, file names, coverage, warnings and actions. Domain imports no other backend package; importers do not depend on processing, sonification, audio or API; persistence depends only on domain. The dependency-rule test enforces the separation.

## Add a source

1. Create `backend/ingestion/<source>/` and implement the `Data_Source_Adapter` interface in `backend/domain/adapter.py`: source identifier, source loading and import result contract. Use existing adapters as concrete examples.
2. Emit normalized domain points and candidate sessions with an IANA-aware timestamp, metric and source unit. Route measurements through the shared validation gate; report unsupported inputs and skips without losing readable files.
3. Register the adapter in the pipeline registry and, if needed, add an explicit UI/CLI import field. Do not reach into another importer's internals.
4. Add synthetic fixtures, import/error/timezone tests and validation properties; run the full dependency-rule suite.

The SensorPush cloud API adapter remains an **unimplemented extension point**. It would fetch data in ingestion and emit the same domain output. Keep credentials in environment variables or an external secret store, never in version control, manifests or logs. The current application never calls a wearable/cloud API.

Future sources or targets such as other wearables, MP3/OGG, mobile applications and live telemetry are outside the MVP. Accounts, AI interpretation, medical advice, alarms and cloud synchronization are also outside scope.
