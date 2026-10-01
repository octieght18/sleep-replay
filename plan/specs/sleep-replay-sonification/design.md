# Design Document

## Overview

The sleep-replay-sonification spec is the second of three specs that make up the Sleep Replay MVP. It takes the outputs of sleep-replay-data-pipeline — the selected SleepSession, Aligned_Timeline, Feature_Series, Coarse_States, Night_Events, Target_Duration, and Input_Fingerprint — and turns them into one coherent ambient composition rendered to a WAV file with a Replay_Manifest, reachable through the CLI. It stops short of any HTTP API or browser UI; those are added by sleep-replay-app.

The design centers on a strict two-stage, one-directional flow with a pure seam in the middle:

```
Feature_Series + Coarse_States + Night_Events + Mapping_Config + Target_Duration + Random_Seed
                        │
                        ▼
        Sonification_Engine  (pure, deterministic)
                        │
                        ▼
   Sound_Parameter trajectories + Transient_Events + Note_Events + gestures + preset plan
                        │
                        ▼
        Audio_Renderer  (deterministic synthesis; no telemetry as audio)
                        │
                        ▼
              float mix ──▶ loudness scaling ──▶ 16-bit PCM WAV + Replay_Manifest
```

The seam between the Sonification_Engine and the Audio_Renderer is the key architectural decision (Requirement 7): the engine derives every Sound_Parameter trajectory and every Transient_Event exclusively from Feature_Series, Coarse_States, Night_Events, the Mapping_Config, the Target_Duration, and the Random_Seed — never from raw TelemetryPoints or Aligned_Timeline samples — and the renderer synthesizes audio exclusively from trajectories and scheduled events, never time-scaling or playing back raw telemetry. This makes Requirement 7.1 a directly testable property of the engine's function signature, and keeps "sonification from features" auditable.

Determinism (Requirement 9) is designed in, not bolted on: all randomness comes from PRNGs seeded from the Random_Seed at generation start, trajectories are represented as exact piecewise-linear breakpoint functions evaluated per sample, and the final mix is rendered to a float buffer, measured, scaled, and quantized with a fixed rounding rule — no wall-clock, locale, or environment input anywhere.

### Design goals and key decisions

| Decision | Rationale |
|---|---|
| Two-stage pipeline with a pure trajectory/event seam | Requirements 7.1/7.2; lets sonification logic be property-tested without rendering audio, and audio rendering be tested with synthetic trajectories |
| All sound from four fixed Sound_Layers (pad, pulse, texture, transient) | Requirement 6.1; one coherent composition instead of per-sensor beeps |
| Metrics influence only shared Sound_Parameters; physiological/environmental metrics never trigger Transient_Events | Requirement 6.2; keeps the soundscape intentional |
| Trajectories are piecewise-linear breakpoint functions in Replay_Time, evaluated per sample | Exact rate limits (6.6) and glide durations (3.7/3.8/5.5/5.6) hold at every pair of Replay_Times, not just at window boundaries; deterministic and cheap |
| One seeded PRNG per generation, sub-streams derived deterministically per purpose | Requirements 9.2, 9.8; independent of evaluation order, reproducible across processes |
| Render to float64, then one global gain pass for loudness, then fixed int16 quantization | Requirements 8.5/8.6 (peak and RMS), 8.9 (finite guard), 9.1 (byte-identical) |
| Mapping_Config types, sound types, and the Replay_Manifest model live in the `domain` package; parsing/printing/serializing logic lives in `sonification` | Keeps `domain` the shared vocabulary (Dependency_Rule (a) preserved); parser/serializer are behavior, models are vocabulary |
| Replay generation orchestration in `sonification`, CLI in `api` | Dependency_Rules: `sonification` may import `processing` and `persistence`; only `api` may also import `ingestion`, which the CLI needs for file import |

## Architecture

### Package structure

This spec fills in the `sonification` and `audio` placeholder packages created by sleep-replay-data-pipeline, adds sound/config/manifest vocabulary to `domain`, and adds the CLI to `api`:

```
backend/
  domain/
    ... (unchanged from sleep-replay-data-pipeline)
    sound.py                  # NEW: Sound_Parameter, Parameter_Trajectory, Transient_Event,
                              #     Note_Event, Onset_Gesture, Render_Plan
    mapping.py                # NEW: Mapping_Config, Metric_Mapping, Mapping_Target, defaults
    replay.py                 # NEW: Replay_Manifest model + Availability entry
  sonification/               # imports domain, processing, persistence; NO ingestion/api
    __init__.py
    config_parser.py          # Mapping_Config_Parser (YAML + JSON, validation, User_Errors)
    config_printer.py         # Mapping_Config_Printer (canonical YAML, fixed key order)
    presets.py                # Soundscape_Preset definitions + per-window assignment plan
    normalize.py              # Normalization_Spans (physiological + environmental)
    contributions.py          # per-metric Mapped_Values, hysteresis, glides
    combine.py                # weighted combination into Sound_Parameter trajectories + rate limits
    transients.py             # movement/Brief_Awakening transient scheduling + cap
    gestures.py               # sleep onset / awakening gestures
    engine.py                 # Sonification_Engine: features → Render_Plan
    manifest.py               # Manifest_Serializer (canonical JSON, parse + validation)
    generation.py             # generate_replay() orchestration + Replay storage + caching hook
  audio/                      # imports sonification, domain; NO ingestion/processing/api
    __init__.py
    layers.py                 # pad, pulse, texture, transient Sound_Layer synthesizers
    envelopes.py              # attack/release envelope helpers (min attack/release)
    scales.py                 # scale/key selection from Random_Seed
    renderer.py               # Audio_Renderer: Render_Plan → float64 mix
    loudness.py               # peak/RMS measurement, gain scaling, int16 quantization
    wav.py                    # deterministic WAV writer (stdlib wave + struct)
  api/
    pipeline.py               # (from sleep-replay-data-pipeline)
    cli.py                    # NEW: sample-data and generate commands
docs/
  mapping-config.example.yaml # NEW: example Mapping_Config (documented in sleep-replay-app)
examples/
  example_replay.wav          # NEW: committed example Replay + manifest
```

The Dependency_Rules from sleep-replay-data-pipeline Requirement 17.2 already permit exactly these imports (`sonification → domain/processing`, `audio → sonification/domain`, `api → everything`). The existing AST-based dependency test (data-pipeline task 1.2) enforces them unchanged; no new rules are added.

### High-level generation sequence

```mermaid
sequenceDiagram
    participant CLI as CLI (api/cli.py)
    participant Pipe as api/pipeline.py
    participant Gen as sonification/generation.py
    participant Eng as Sonification_Engine
    participant Rend as Audio_Renderer
    participant Ser as Manifest_Serializer
    participant Store as persistence

    CLI->>Pipe: import files, select session, process(target)
    Pipe-->>CLI: session, Feature_Series, Coarse_States, Night_Events, Processing_Report
    CLI->>Gen: generate_replay(inputs, Mapping_Config, seed)
    Gen->>Gen: resolve seed (CLI > config > default), validate
    Gen->>Eng: build Render_Plan(features, states, events, config, target, seed)
    Eng-->>Gen: Render_Plan (trajectories, events, preset plan)
    Gen->>Rend: render(Render_Plan)
    Rend-->>Gen: float mix (finite checked)
    Gen->>Gen: loudness scale + quantize → PCM
    Gen->>Ser: serialize(Replay_Manifest)
    Ser-->>Gen: canonical JSON bytes
    Gen->>Store: atomic write WAV + manifest; record Replay in Metadata_Store
    Store-->>Gen: ok / User_Error (partial files removed on failure)
    Gen-->>CLI: paths, warnings
```

## Components and Interfaces

### Mapping_Config_Parser (sonification/config_parser.py)

Parses and validates Mapping_Config documents (Requirement 1).

- Accepts UTF-8 documents up to 64 KB in YAML or JSON format (1.1). Format is detected by a first-pass JSON parse; on failure, the YAML parser is used.
- Per metric key (`heart_rate`, `hrv`, `movement`, `temperature`, `humidity`, `pressure`), matched case-sensitively: `target` one of `pulse_rate`, `rhythmic_density`, `intensity`, `transient_density`, `brightness`, `texture_density`, `modulation`, `none`; `sensitivity` a number in [0.0, 1.0] (1.2).
- For `heart_rate`, `hrv`, `temperature`, `humidity`, `pressure` only: optional `smoothing_minutes` (whole minutes of Night_Time, 1–60) and `hysteresis` in the metric's Canonical_Unit within the allowed ranges (heart_rate 0–10 bpm; hrv 0–20 ms; temperature 0–1.0 °C; humidity 0–5 pp; pressure 0–1.0 hPa) (1.3).
- Optional global `target_duration` ∈ {30, 120, 180, 300, 600} seconds and `random_seed` ∈ [0, 4,294,967,295], both integers (1.4).
- Defaults are filled per omitted value individually (1.5): Default_Mapping target/sensitivity per metric, smoothing window and Hysteresis_Threshold from the Default Parameter Values, target_duration 180, default Random_Seed. An empty YAML document or empty JSON object yields the full default Mapping_Config.
- Every rejection returns a User_Error (code `INVALID_MAPPING_CONFIG`) giving the 1-based line number for syntax errors or the full key path for key/value errors, and listing the allowed keys, targets, value range, or size limit (1.6). Rejection applies none of the document's values and leaves the previously applied config unchanged (1.7). Type strictness: booleans and quoted numbers are rejected where a number is expected; non-integers where an integer is expected; duplicate and unknown/misplaced keys (including smoothing/hysteresis under `movement`) are rejected (1.6).
- YAML parsing uses a pinned PyYAML version with `SafeLoader`; duplicate keys are detected with a custom loader constructor (PyYAML does not flag them by default).

### Mapping_Config_Printer (sonification/config_printer.py)

Formats any valid Mapping_Config as a canonical YAML document (1.8): all six metric keys with every accepted field (defaults applied), `target_duration`, `random_seed`, fixed key order, fixed number formatting (integers without decimal point, sensitivities with fixed decimals), LF newlines. Equivalent configs produce byte-identical documents, which makes the parse→print→parse round-trip (1.9) a clean property.

### Soundscape_Presets (sonification/presets.py)

Assigns exactly one Soundscape_Preset per Feature_Window from its Coarse_State (2.1): awake→Awake, light→Light, deep→Deep, rem→REM, asleep→Light, restless→Light + Restless_Texture_Boost × movement Sensitivity (clamped to 1.0, movement Sensitivity counted as 0.0 when its target is `none`, applied once even where 4.4 also applies), unknown→Neutral (2.2–2.8).

Each preset is a parameter set for the four Sound_Layers: pad chord voicing and harmonic content, texture noise color and density scale, modulation rate/depth on pad and texture, and note-onset behavior. The preset constants are tuned so the measurable characters hold on Preset_Comparison_Renders (120 s, all Sound_Parameters at 0.5, no transients or gestures, default seed, analysis interval 5–115 s):

- **Deep** vs **Light**: Spectral_Centroid ≤ 0.8×, Low_Frequency_Share ≥ +0.10, lower Preset_Modulation_Rate (2.9).
- **REM**: Preset_Modulation_Depth ≥ 0.2 above every other preset (2.10).
- **Awake**: ≥3 note onsets, Note_Event_Density ≤ 0.5× Light's, onset-interval CV ≥ 0.3 (2.11).
- **Neutral**: Spectral_Centroid, Low_Frequency_Share, and Preset_Modulation_Rate each between Light's and Deep's inclusive (2.13).

Preset changes between consecutive Feature_Windows crossfade centered on the window boundary for min(Stage_Crossfade_Duration, the Replay_Time length of the shorter adjacent equal-preset run), outgoing gain monotonically full→zero, incoming zero→full (2.12). The preset plan emitted by the engine is a list of (Replay_Time start, end, preset) segments plus crossfade durations; the renderer evaluates per-layer gains from it. Only the four Sound_Layers sound during crossfades (6.1).

### Normalization (sonification/normalize.py)

Computes per-session Normalization_Spans from the smoothed, non-missing Feature_Series values (5.4, 3.1, 3.4):

- Environmental (temperature, humidity, pressure): span centered on the midpoint of [min, max] smoothed values, width = max(max − min, environmental minimum span: 2.0 °C, 10 pp, 3.0 hPa). Normalization maps span bounds to 0.0/1.0, so a difference Δ normalizes to Δ / width (5.4), independent of Display_Units.
- Physiological (heart_rate, hrv_rmssd): same construction with the physiological minimum spans (10 bpm, 10 ms) (3.1, 3.4).
- Constant Session_HRV (no in-session hrv_rmssd): one constant contribution for every window, non-decreasing in Session_HRV (3.5). No hrv data at all: Neutral_Value for the whole Replay (3.6).

### Contributions, hysteresis, and glides (sonification/contributions.py)

For each metric and Feature_Window, computes the Mapped_Value: `0.5 + sensitivity × (normalized − 0.5)` clamped to [0, 1], where the Neutral_Value is 0.5 (6.10). This gives direction and the sensitivity monotonicity property (6.11) by construction: |contribution − 0.5| scales with sensitivity. A target of `none` or sensitivity 0.0 holds the contribution at 0.5 so the metric leaves every trajectory unchanged (6.10).

- **Pressure cap**: the absolute difference between the pressure contribution and the Neutral_Value never exceeds 0.25 for any sensitivity in [0, 1] (5.3) — implemented as a ×0.5 scale on the pressure excursion.
- **Hysteresis**: per metric, a Hysteresis_Reference initialized to the first non-missing smoothed value. While |smoothed − reference| < Hysteresis_Threshold the contribution holds its last applied value (6.4); on reaching/exceeding the threshold the contribution updates from the current smoothed value and the reference is set to it (6.5).
- **Missing-data glides**: when a metric's features become missing after non-missing, the contribution glides monotonically from its current value to the Neutral_Value over 1–2 s of Replay_Time (physiological: at least 1 s per 3.7; environmental: 1–2 s per 5.5) starting at the missing window's beginning, then holds at Neutral_Value. When features return, the contribution glides from its current value (including mid-glide values) to the Mapped_Value over 1–2 s (3.8, 5.6). Missing in the first window holds Neutral_Value from Replay_Time 0 (5.7).
- **Movement**: Movement_Intensity is already in [0, 1] per window; its contribution follows the same Mapped_Value form with normalized = Movement_Intensity.

### Combination and rate limits (sonification/combine.py)

- Metrics targeting the same Sound_Parameter combine as the sensitivity-weighted mean of their contributions, unavailable/missing metrics entering at the Neutral_Value with their sensitivity as weight (6.8). A parameter with no targeting metric, or all-zero sensitivity weights, sits at the Neutral_Value (6.9).
- Trajectories are piecewise-linear breakpoint functions of Replay_Time. Per-window target values become breakpoints at window centers with linear ramps between; glides and the restless raise add explicit ramp segments with their required durations.
- A final **rate-limit pass** walks each trajectory chronologically and clamps slopes to the parameter's rate limit (fast: pulse_rate, rhythmic_density, intensity, transient_density at 0.5 of range per second; slow: brightness, texture_density, modulation at 0.2 per second of Replay_Time) (6.6). Because clamping is monotone-chronological and total, the |p(t2) − p(t1)| ≤ rate × (t2 − t1) property holds for every pair of Replay_Times regardless of cause. Every value stays in [0.0, 1.0] (6.7 — invariant, property-tested).

### Transient scheduling (sonification/transients.py)

- **Movement transients**: where the movement target is not `none` and sensitivity > 0, each Feature_Window with Movement_Intensity > Movement_Transient_Threshold schedules at least one Transient_Event with onset inside the window's Replay_Time span (4.1); windows at or below the threshold schedule none (4.7). Amplitude ∈ (0, 1], non-decreasing in Movement_Intensity for fixed sensitivity and in sensitivity for fixed intensity (4.2): amplitude = sensitivity × intensity, clamped to (0, 1].
- **Restless periods**: while a Feature_Window lies within a Restless_Period, texture_density and transient_density are raised above their un-raised values by an amount > 0 proportional to movement sensitivity, subject to [0, 1] and rate limits (4.4).
- **Brief_Awakenings**: a Brief_Awakening shorter than Minimum_State_Duration × Compression_Ratio renders as one Transient_Event at (start − session_start) / Compression_Ratio instead of a Coarse_State change (4.6); these are scheduled even when movement sensitivity is 0 or its target is `none` (4.8).
- **Cap**: any 1-second Replay_Time interval holds onsets of at most Max_Transients_Per_Second (4); overflow candidates are omitted lowest-amplitude first, later onset first on ties (4.5, 4.9).

### Gestures (sonification/gestures.py)

Each sleep onset and awakening Night_Event renders as a gesture: duration ≥ 2 s of Replay_Time, containing the event's Replay_Time, entirely within [0, Target_Duration], envelope attack and release ≥ 0.5 s each, and no Transient_Event (6.12). Onset = a slow downward pad swell; awakening = a slow brightening swell. Clamped inward when the event sits near an endpoint.

### Sonification_Engine (sonification/engine.py)

Pure function: `(Feature_Series, Coarse_States, Night_Events, Mapping_Config, Target_Duration, Random_Seed) → Render_Plan`. Assembles normalization, contributions, combination, preset plan, transients, and gestures. Takes no telemetry, timeline, session, clock, or I/O — which is what makes Requirement 7.1 a property of the function itself. All PRNG use inside the engine comes from a sub-stream seeded `Random(f"{seed}:engine")`.

### Audio_Renderer (audio/renderer.py, layers.py, loudness.py, wav.py)

Deterministic synthesizer: `render(Render_Plan, Random_Seed) → float64 stereo buffer`.

- **Scale and key**: exactly one scale (pentatonic major) and one key per Replay, derived from the Random_Seed at render start and unchanged for the whole Replay; every pitched note's nominal pitch belongs to that scale and key (6.3).
- **Sound_Layers** (6.1):
  - *pad*: additive chord tones from the scale/key; brightness controls harmonic rolloff; slow preset modulation applies to gain/timbre.
  - *pulse*: soft low sine pulses; pulse rate derived from pulse_rate so 0.0 → 0.5 pulses/s, 1.0 → 2.0 pulses/s, monotone non-decreasing between, independent of bpm (3.2).
  - *texture*: filtered noise (noise color per preset); texture_density controls band density/gain.
  - *transient*: short enveloped blips for Transient_Events; attack ≥ 5 ms, total duration 50 ms–1.5 s (4.3).
- **Sound_Parameter → layer mapping**: pulse_rate → pulse rate; rhythmic_density → pulse subdivision density; intensity → overall pad+texture gain; transient_density → texture grain rate; brightness → pad harmonic content; texture_density → texture density; modulation → pad/texture modulation depth.
- **Envelopes**: every Note_Event and Transient_Event rises zero→peak over ≥ 5 ms and falls to zero over ≥ 20 ms (8.4).
- **Randomness**: per-layer sub-streams `Random(f"{seed}:pad")` etc., created at render start (9.2). Same seed → same audio; different seed → identical format and length with at least one differing sample (9.5).
- **Continuity**: oscillators are phase-continuous; parameter-driven changes are per-sample evaluated from the breakpoint trajectories; preset crossfades are gain ramps. The renderer asserts |sample[n] − sample[n−1]| < 0.3 of full scale per channel as a post-condition (8.8) and rejects any non-finite sample before quantization (8.9, 8.13).
- **Fades**: fade-in 0.0 → 1.0 over [0, 2.0] s, fade-out 1.0 → 0.0 over the last 3.0 s, both monotonic; first and last samples of each channel ≤ 0.001 full scale (8.3).
- **Length**: exactly Target_Duration × 44,100 frames (7.3); duration within 50 ms by construction (8.2).
- **Loudness** (loudness.py): measure overall RMS and peak of the float mix; apply one global gain so RMS lands at −22 dBFS (the midpoint of −28…−16); if the resulting peak exceeds 0.891 (−1 dBFS), reduce gain to the peak limit. The layer mix is designed so the crest factor keeps both constraints satisfiable; a post-condition asserts peak ≤ 0.891 and RMS ∈ [−28, −16] dBFS (8.5, 8.6) and per-second RMS ≥ −50 dBFS over each whole-second window in [2.0 s, end − 3.0 s] (8.7).
- **Quantization + WAV** (wav.py): clamp to [−1, 1], convert with fixed round-half-away-from-zero to int16 (deterministic, platform-independent), write 44,100 Hz / 16-bit / 2-channel PCM via stdlib `wave` + `struct` (8.1), producing byte-identical output for identical float buffers.

### Manifest_Serializer (sonification/manifest.py)

Serializes Replay_Manifests to UTF-8 JSON and parses them back (Requirement 10).

- Serialization is canonical: `json.dumps` with sorted keys, fixed separators, `ensure_ascii=False`, fixed float representation (`repr`-shortest round-trip, which is exact for IEEE doubles), LF newline. Manifests equal in every field serialize byte-identically (10.2).
- Content (10.1): session start/end, Display_Timezone (IANA name), Target_Duration, Compression_Ratio, Timeline_Resolution, Coarse_State segments (start/end Replay_Time + state, ordered, non-overlapping, gap-free over [0, Target_Duration], 10.8), every retained Night_Event (type, Night_Time, Replay_Time, magnitude, label), per-Feature_Window environmental means in Canonical_Units with explicit missing indicators, per-metric availability (status + missing fraction; hrv unavailable only when every sample missing *and* no Session_HRV), Unavailable_Metric keys (11.4), effective Mapping_Config with defaults, Random_Seed used (9.3), Input_Fingerprint, software version, and all warnings as plain-language descriptions. TelemetryPoints and Aligned_Timeline samples are excluded (10.5); generation time lives only in the Metadata_Store Replay record (10.6).
- Timestamps are ISO 8601 with seconds and the Display_Timezone UTC offset at that instant; Replay_Times, Target_Duration, Timeline_Resolution in seconds (10.7).
- Parsing validates (10.9): invalid JSON, a missing field, a magnitude outside [0, 1], a Replay_Time outside [0, Target_Duration], or malformed Coarse_State segments → error identifying at least one missing/invalid field, returning no manifest. Parse(serialize(m)) == m exactly — same instant and offset per timestamp, exactly equal numerics (10.4 — round-trip property).

### Replay generation and storage (sonification/generation.py)

`generate_replay(session, features, states, events, timeline_stats, config, seed, stores)`.

- Seed precedence: CLI option > Mapping_Config `random_seed` > default Random_Seed; the used value is recorded in the manifest (9.3). Out-of-range seeds are rejected with `INVALID_RANDOM_SEED` and no Replay (9.9).
- Graceful degradation (Requirement 11): sessions with Stage_Data, ≥1 heart_rate point, or ≥1 environmental point always render a conforming Replay (11.1); otherwise `NO_USABLE_DATA` naming Fitbit sleep/heart-rate files or a SensorPush CSV, with no WAV and no Replay record (11.2). Unavailable_Metrics hold at Neutral_Value for the whole Replay (11.3) and are listed in the manifest (11.4). No Stage_Data → Neutral preset throughout, metric mappings still active, plus a stage-data warning (11.5, 11.8). Fitbit-only → SensorPush-absent warning (11.6); SensorPush-only (manual session) → Fitbit-absent warning (11.7).
- Storage: WAV and manifest are written inside the Data_Directory via atomic temp-file-then-rename before generation is reported complete (8.12); a write failure deletes partial files of that Replay, leaves earlier Replays unchanged, and returns `REPLAY_WRITE_FAILED` naming the Data_Directory (8.14). A non-finite sample aborts before any write with `RENDERING_FAILED` (8.13).
- The Replay record (id, wav path, manifest path, generation time, Input_Fingerprint, software version, effective Mapping_Config JSON) is written to the Metadata_Store (1.11, 10.6).
- Performance: the renderer works in fixed-size blocks with precomputed oscillator phases, targeting ≤ 60 s for 30 s–3 min Replays and ≤ 180 s for 5–10 min Replays on the Reference_Machine (8.10, 8.11).

### CLI (api/cli.py)

Two commands (Requirement 12), implemented over `api/pipeline.py` and `sonification/generation.py`:

- `sample-data --out DIR [--seed N]`: writes Sample_Data_Generator output into DIR (created if missing), replacing same-named files only, leaving other files untouched; with no seed the files are byte-identical to the committed Sample_Dataset (12.1).
- `generate PATHS... [options]`: accepts Fitbit files or one zip and SensorPush CSVs (at least one path, no Max_Upload_Size limit, 12.2); options: `--config` (YAML/JSON), `--target-duration`, `--seed`, per-file-type Source_Timezone overrides, `--session-date` (YYYY-MM-DD) or `--manual-start/--manual-end` (mutually exclusive with session date; offset-less times interpreted in Display_Timezone), `--output WAV` (manifest written next to it with the same stem, replacing existing files) (12.2).
- Session selection: Session_Detector defaults without manual times (12.3); with `--session-date`, the most recent main-sleep candidate ending on that date in Display_Timezone, else the longest candidate ending that date (12.3); no candidate ending that date → User_Error listing available end dates (12.8). Manual ranges go through the Session_Detector's validation (12.7).
- Settings precedence: command-line option > Mapping_Config file > defaults (3 min, default seed, Data_Directory output); persisted Metadata_Store settings are ignored (12.6).
- Success: exit 0, absolute paths of written files on stdout, plus session start/end with offset, Compression_Ratio to one decimal, Night_Event count, one warning per line (12.4). Failure: stderr gets code, description, file name, action, and reference id for internal errors; output paths left unchanged; non-zero exit (12.5).

### Example Replay (examples/)

The Repository ships `examples/example_replay.wav` + manifest generated from the Sample_Dataset with the Default_Mapping (Requirement 13.1), plus the documented command (Target_Duration, Random_Seed, Display_Timezone stated) that regenerates a byte-identical file.

## Data Models

New domain vocabulary (domain/sound.py, domain/mapping.py, domain/replay.py). All frozen dataclasses, consistent with the pipeline's domain style.

```python
class Mapping_Target(str, Enum):
    pulse_rate="pulse_rate"; rhythmic_density="rhythmic_density"; intensity="intensity"
    transient_density="transient_density"; brightness="brightness"
    texture_density="texture_density"; modulation="modulation"; none="none"

SOUND_PARAMETERS = [t for t in Mapping_Target if t is not Mapping_Target.none]
FAST_PARAMETERS = {pulse_rate, rhythmic_density, intensity, transient_density}   # 0.5/s
SLOW_PARAMETERS = {brightness, texture_density, modulation}                       # 0.2/s

@dataclass(frozen=True)
class Metric_Mapping:
    target: Mapping_Target
    sensitivity: float              # [0.0, 1.0]
    smoothing_minutes: int | None   # 1..60; None for movement (rejected there)
    hysteresis: float | None        # Canonical_Unit; None for movement

@dataclass(frozen=True)
class Mapping_Config:
    metrics: dict[str, Metric_Mapping]   # all six keys, defaults applied after parse
    target_duration: int                 # {30,120,180,300,600}
    random_seed: int                     # [0, 2**32 - 1]

DEFAULT_MAPPING = Mapping_Config(...)    # Default_Mapping + default parameter values
```

```python
@dataclass(frozen=True)
class Breakpoint:           # Parameter_Trajectory = piecewise-linear breakpoints
    t: float                # Replay_Time seconds, ascending
    value: float            # [0.0, 1.0]

Parameter_Trajectory = tuple[Breakpoint, ...]

@dataclass(frozen=True)
class Transient_Event:
    onset_s: float          # Replay_Time
    amplitude: float        # (0, 1]
    kind: str               # "movement" | "brief_awakening"

@dataclass(frozen=True)
class Note_Event:
    onset_s: float; duration_s: float; pitch_hz: float; amplitude: float; layer: str

@dataclass(frozen=True)
class Onset_Gesture:
    event_type: str         # "sleep_onset" | "awakening"
    start_s: float; end_s: float   # >= 2 s, within [0, Target_Duration], contains event time

@dataclass(frozen=True)
class Preset_Span:
    start_s: float; end_s: float; preset: str; crossfade_in_s: float

@dataclass(frozen=True)
class Render_Plan:          # the engine → renderer seam (Requirement 7)
    target_duration_s: int
    trajectories: dict[Mapping_Target, Parameter_Trajectory]
    transients: tuple[Transient_Event, ...]
    gestures: tuple[Onset_Gesture, ...]
    preset_plan: tuple[Preset_Span, ...]
    seed: int
```

```python
@dataclass(frozen=True)
class Night_Event_Record:   # manifest form of a Night_Event
    type: str; night_time: datetime; replay_time_s: float; magnitude: float; label: str

@dataclass(frozen=True)
class Metric_Availability:
    status: str             # "available" | "unavailable"
    missing_fraction: float # [0.0, 1.0]

@dataclass(frozen=True)
class Replay_Manifest:
    session_start: datetime; session_end: datetime
    display_timezone: str
    target_duration_s: int; compression_ratio: float; timeline_resolution_s: int
    coarse_states: tuple[tuple[float, float, str], ...]   # start, end, state; gap-free cover
    night_events: tuple[Night_Event_Record, ...]
    environmental_windows: tuple[tuple[float | None, float | None, float | None], ...]
    availability: dict[str, Metric_Availability]          # six metrics
    unavailable_metrics: tuple[str, ...]                  # Mapping_Config keys (Req 11.4)
    mapping_config: Mapping_Config                        # effective, defaults applied
    random_seed: int
    input_fingerprint: str
    version: str
    warnings: tuple[str, ...]
```

New error codes added to the domain table: `INVALID_MAPPING_CONFIG`, `INVALID_RANDOM_SEED`, `NO_USABLE_DATA`, `RENDERING_FAILED`, `REPLAY_WRITE_FAILED`, `GENERATION_IN_PROGRESS` (the last enforced by the Backend in sleep-replay-app; the code is reserved here).

## Correctness Properties

*A property is a characteristic or behavior that should hold true across all valid executions of a system — essentially, a formal statement about what the system should do. Properties serve as the bridge between human-readable specifications and machine-verifiable correctness guarantees.*

Sonification is again a strong fit for property-based testing: the parser/printer, contribution math, hysteresis, combination, rate limiting, and the manifest serializer are pure functions, and the requirements label several round-trip, monotonicity, invariant, and confluence properties. Render-involving properties run fewer cases (≥5 per Requirement 14.2) because each case renders audio.

### Property 1: Mapping_Config parse-print-parse round trip

*For any* valid Mapping_Config document (YAML or JSON), parsing, printing, and parsing again produces an equivalent Mapping_Config: numerically equal target, sensitivity, smoothing window, and Hysteresis_Threshold per metric key, and equal target_duration and random_seed.

**Validates: Requirements 1.9**

### Property 2: Mapping_Config format equivalence

*For any* valid Mapping_Config, a JSON document and a YAML document expressing the same keys and values parse to equivalent Mapping_Configs.

**Validates: Requirements 1.10**

### Property 3: Physiological contribution monotonicity

*For all* pairs of smoothed values v1 ≤ v2 of heart rate or hrv_rmssd within a SleepSession with the corresponding sensitivity above 0.0, the contribution computed for v1 is less than or equal to that computed for v2, evaluated before hysteresis, rate limiting, and combination.

**Validates: Requirements 3.3**

### Property 4: Pulse rate bounds and monotonicity

*For all* pulse_rate Sound_Parameter values in [0.0, 1.0], the derived pulse event rate lies within the pulse rate range (0.5–2.0 pulses/s), equals the lower bound at 0.0 and the upper bound at 1.0, and is non-decreasing in pulse_rate.

**Validates: Requirements 3.2**

### Property 5: Missing-feature glide behavior

*For any* metric and any Feature_Window sequence, when features become missing after non-missing the contribution glides monotonically from its current value to the Neutral_Value over at least 1 s (and at most 2 s) of Replay_Time starting at the missing window and holds there while missing; when features return it glides from its current value (including mid-glide values) to the Mapped_Value over at least 1 s (at most 2 s for environmental metrics); features missing in the first window hold the Neutral_Value from Replay_Time 0.

**Validates: Requirements 3.7, 3.8, 5.5, 5.6, 5.7**

### Property 6: Movement transient amplitude monotonicity

*For all* Movement_Intensity values and movement sensitivities, every movement Transient_Event amplitude lies in (0.0, 1.0], is non-decreasing in Movement_Intensity for fixed sensitivity, and is non-decreasing in sensitivity for fixed Movement_Intensity.

**Validates: Requirements 4.2**

### Property 7: Transient density cap

*For any* Feature_Series and Brief_Awakening set, after scheduling, every 1-second Replay_Time interval contains onsets of at most Max_Transients_Per_Second Transient_Events, and omitted candidates are those of lowest amplitude (later onset first on ties).

**Validates: Requirements 4.5, 4.9**

### Property 8: Environmental normalization linearity

*For any* environmental metric, normalization over its Normalization_Span maps the span bounds to 0.0 and 1.0, and a difference Δ between two smoothed values produces a normalized difference of exactly Δ divided by the span width.

**Validates: Requirements 5.4**

### Property 9: Environmental contribution monotonicity

*For all* pairs of smoothed values v1 ≤ v2 of temperature, humidity, or pressure with sensitivity above 0.0, the Mapped_Value of v1 is less than or equal to that of v2.

**Validates: Requirements 5.8**

### Property 10: Pressure contribution cap

*For all* smoothed pressure values and all pressure sensitivities in [0.0, 1.0], the absolute difference between the pressure contribution and the Neutral_Value never exceeds the pressure maximum contribution (0.25).

**Validates: Requirements 5.3**

### Property 11: Hysteresis hold and update

*For any* sequence of smoothed values, while the absolute difference from the Hysteresis_Reference is below the Hysteresis_Threshold the contribution holds its last applied value; when the difference reaches or exceeds the threshold the contribution updates from the current value and the reference becomes the current value.

**Validates: Requirements 6.4, 6.5**

### Property 12: Sound_Parameter rate limit

*For all* valid Mapping_Configs and Feature_Series, each Sound_Parameter trajectory satisfies |p(t2) − p(t1)| ≤ rate_limit × (t2 − t1) for every pair of Replay_Times t1 < t2, whatever the cause of the change.

**Validates: Requirements 6.6**

### Property 13: Sound_Parameter bounds invariant

*For all* valid Mapping_Configs and SleepSessions, every Sound_Parameter value at every Replay_Time lies within 0.0 to 1.0 inclusive.

**Validates: Requirements 6.7**

### Property 14: Sensitivity monotonicity

*For all* fixed metric values and sensitivities s1 ≤ s2, the absolute deviation of the contribution from the Neutral_Value at s2 is greater than or equal to that at s1.

**Validates: Requirements 6.11**

### Property 15: Sonification from features only

*For any* two SleepSessions with identical Feature_Series, Coarse_States, and Night_Events, the Sonification_Engine produces identical Sound_Parameter trajectories and Transient_Events under the same Mapping_Config, Target_Duration, and Random_Seed.

**Validates: Requirements 7.1**

### Property 16: WAV frame count

*For any* valid Target_Duration, a completed Replay WAV contains exactly Target_Duration × 44,100 sample frames, with the fade-in and fade-out contained within that length.

**Validates: Requirements 7.3, 8.2**

### Property 17: Audio output bounds

*For any* rendered Replay, the WAV is 44,100 Hz / 16-bit / 2-channel; every sample of each channel is finite with absolute value ≤ 0.891 of full scale; overall RMS lies between −28 and −16 dBFS inclusive; every non-overlapping 1-second window from 2.0 s to 3.0 s before the last sample has RMS ≥ −50 dBFS; consecutive same-channel samples differ by less than 0.3 of full scale; first and last samples of each channel are within 0.001 of zero.

**Validates: Requirements 8.1, 8.3, 8.5, 8.6, 8.7, 8.8, 8.9**

### Property 18: Replay determinism and confluence

*For any* Generation_Inputs, generating twice on the same machine and installation produces byte-identical WAV files and byte-identical manifests, including for every permutation of input file order; and two Generation_Inputs differing only in Random_Seed produce manifests identical except in the seed fields, with WAV files of identical format and length differing in at least one sample.

**Validates: Requirements 9.1, 9.4, 9.5, 9.6**

### Property 19: Replay_Manifest serializer round trip

*For all* valid Replay_Manifests, serializing then parsing produces a manifest equal in every field, each timestamp representing the same instant and UTC offset and each numeric value exactly equal.

**Validates: Requirements 10.4**

### Property 20: Graceful degradation loudness

*For any* selected SleepSession having Stage_Data, at least one heart_rate point, or at least one environmental point, and any subset of Unavailable_Metrics, the generated Replay meets the audio format and loudness limits (peak ≤ −1 dBFS, overall RMS ∈ [−28, −16] dBFS).

**Validates: Requirements 11.1**

## Error Handling

Sonification follows the pipeline's principle: actionable failures become structured `User_Error`s with stable codes; recoverable conditions become warnings recorded in the Replay_Manifest.

### Error vs warning decision

- **Stop with a User_Error**: invalid Mapping_Config (`INVALID_MAPPING_CONFIG`, with line number or key path and allowed values, 1.6); invalid Random_Seed (`INVALID_RANDOM_SEED`, allowed range, 9.9); session with no usable data (`NO_USABLE_DATA`, naming the files to import, 11.2); non-finite rendered sample (`RENDERING_FAILED`, no WAV/manifest written, 8.13); WAV/manifest write failure (`REPLAY_WRITE_FAILED`, partial files removed, Data_Directory named, 8.14); CLI argument violations (12.5).
- **Continue with a warning** (recorded in the Replay_Manifest, 10.1): SensorPush environmental data absent (11.6), Fitbit data absent (11.7), stage data unavailable / Neutral preset used (11.8). Unavailable_Metrics are listed by key, not warned (11.4).

### Atomicity

A rejected Mapping_Config applies none of its values (1.7). A failed generation leaves previously stored Replays, settings, and imports unchanged: rendering failures abort before any write, and storage uses temp-file + atomic rename per artifact with partial-file cleanup on failure (8.13, 8.14). The CLI never writes to the output path on failure (12.5).

## Testing Strategy

Property-based tests (Hypothesis) cover the universal properties; example/edge-case tests pin the preset characters, gesture shapes, CLI behavior, and degradation messages; integration tests cover end-to-end generation, determinism across processes, and performance (Requirement 14).

### Tooling

- `pytest` + Hypothesis, same suite and command as sleep-replay-data-pipeline (`python -m pytest`).
- Audio assertions read the produced WAV with stdlib `wave` + `struct`/`audioop`-style helpers; spectral measurements (Spectral_Centroid, Low_Frequency_Share) use `numpy` FFT over the analysis interval.
- Preset_Comparison_Render helper builds a 120 s render at all parameters 0.5 with the default seed for the preset character tests (2.9–2.13).

### Property-based tests

One test per property, tagged `# Feature: sleep-replay-sonification, Property N: ...`, ≥100 cases except render-involving properties (≥5):

| Property | Requirement(s) | Min cases |
|---|---|---|
| 1 Config round trip | 1.9 | 100 |
| 2 Format equivalence | 1.10 | 100 |
| 3 Physiological monotonicity | 3.3 | 100 |
| 4 Pulse rate bounds/monotonicity | 3.2 | 100 |
| 5 Missing-feature glide | 3.7, 3.8, 5.5–5.7 | 100 |
| 6 Transient amplitude monotonicity | 4.2 | 100 |
| 7 Transient density cap | 4.5, 4.9 | 100 |
| 8 Normalization linearity | 5.4 | 100 |
| 9 Environmental monotonicity | 5.8 | 100 |
| 10 Pressure cap | 5.3 | 100 |
| 11 Hysteresis hold/update | 6.4, 6.5 | 100 |
| 12 Rate limit | 6.6 | 100 |
| 13 Parameter bounds invariant | 6.7 | 100 |
| 14 Sensitivity monotonicity | 6.11 | 100 |
| 15 Features-only determinism | 7.1 | 100 |
| 16 WAV frame count | 7.3, 8.2 | 5 |
| 17 Audio output bounds | 8.1–8.9 | 5 |
| 18 Determinism + confluence | 9.1, 9.4–9.6 | 5 |
| 19 Manifest round trip | 10.4 | 100 |
| 20 Degradation loudness | 11.1 | 5 |

### Example and edge-case tests

At least one test per area in Requirement 14.1: Mapping_Config parsing/validation, sonification mappings, graceful degradation, deterministic audio generation, and the CLI.

- Parser: each accepted/rejected key, value type, and range (1.2–1.4); empty document and empty JSON object defaults (1.5); duplicate key; smoothing/hysteresis misplaced under `movement`; boolean/quoted-number rejection; 64 KB limit; line number and key path in errors; rejection leaves previous config unchanged (1.6, 1.7).
- Presets: Preset_Comparison_Render measurements for Deep (2.9), REM (2.10), Awake (2.11), Neutral (2.13); crossfade monotonicity and duration shortening at short runs (2.12); restless boost applied once with movement target `none` (2.7).
- Mappings: constant Session_HRV contribution and ordering (3.5); hrv Neutral_Value with no data (3.6); movement threshold inclusion/exclusion (4.1, 4.7); restless raise above un-raised values (4.4); Brief_Awakening transient placement (4.6); sensitivity 0 / target none (4.8, 6.10); weighted-mean combination and all-zero weights (6.8, 6.9).
- Renderer: envelope attack/release minima (4.3, 8.4); gesture duration/containment/no-transient (6.12); single scale/key with in-scale pitches (6.3); fade endpoints (8.3); non-finite sample abort (8.13); write failure cleanup (8.14).
- Manifest: required fields (10.1); no telemetry samples (10.5); generation time only in the record (10.6); timestamp formats (10.7); Coarse_State coverage (10.8); each rejection case (10.9).
- Degradation: no-usable-data error naming the files (11.2); Unavailable_Metric listing (11.4); Neutral-preset warning (11.8); Fitbit-only and SensorPush-only warnings (11.6, 11.7).
- CLI: sample-data byte-identity without seed and directory behavior (12.1); argument validation and precedence (12.2, 12.6); session-date selection and no-match error listing end dates (12.3, 12.8); manual-range selection (12.7); stdout/stderr contract and exit codes (12.4, 12.5).

### Integration and smoke tests

- **End-to-end** (14.3): import Sample_Dataset, generate 3 min Replay with Default_Mapping and default seed; assert format, duration within 50 ms, loudness limits, manifest parses, 1–12 Night_Events.
- **Determinism** (14.4): two identical generations → byte-identical WAV and manifest; different seed → different samples, identical Coarse_State segments and Night_Events.
- **Cross-process** (9.8): render in a fresh process after import into an empty Data_Directory matches a render in a process that already rendered other Replays.
- **Environment independence** (9.7): outputs unchanged across wall-clock, locale, host timezone (explicit Display_Timezone), output path, Data_Directory location, and Display_Units.
- **Performance** (8.10, 8.11): 30 s/2 min/3 min Replays stored within 60 s; 5/10 min within 180 s on the Reference_Machine.
- **Example Replay** (13.1): the documented command regenerates a byte-identical `examples/example_replay.wav`.

### Suite constraints (Requirement 14.5–14.7)

Same command, inputs, temp-directory, network-disabled, and non-modification rules as the data-pipeline suite; render-heavy properties are capped at 5 cases and short Target_Durations (30 s) keep the suite within 5 minutes on the Reference_Machine.
