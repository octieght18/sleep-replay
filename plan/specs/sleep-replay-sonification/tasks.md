# Implementation Plan: sleep-replay-sonification

## Overview

Sonification is built in Python on top of the completed data pipeline, in the order of the design's two-stage flow: first the new domain vocabulary (sound types, Mapping_Config model, Replay_Manifest), then the Mapping_Config_Parser and Printer, then the pure Sonification_Engine pieces (normalization, contributions, hysteresis, glides, combination, rate limits, transients, gestures, presets), then the Audio_Renderer and loudness stage, the Manifest_Serializer, the generation orchestration with storage and graceful degradation, and finally the CLI, the determinism/performance tests, and the committed example Replay. Property tests from design.md sit next to the code they check. Testing uses pytest and Hypothesis; audio is asserted with stdlib `wave` readers and numpy spectral helpers.

## Tasks

- [ ] 1. Implement the sonification domain vocabulary
  - [ ] 1.1 Implement sound types
    - `backend/domain/sound.py`: `Mapping_Target` enum, `SOUND_PARAMETERS`, `FAST_PARAMETERS`/`SLOW_PARAMETERS` with the parameter rate limits, frozen `Breakpoint`, `Parameter_Trajectory` with `value_at(t)` (piecewise-linear, clamped to endpoints), `Transient_Event`, `Note_Event`, `Onset_Gesture`, `Preset_Span`, and `Render_Plan`
    - _Requirements: 6.6, 7.1_
  - [ ] 1.2 Implement the Mapping_Config model and defaults
    - `backend/domain/mapping.py`: frozen `Metric_Mapping` and `Mapping_Config`, the six metric keys, `DEFAULT_MAPPING` (Default_Mapping targets/sensitivities, default smoothing windows and Hysteresis_Thresholds, target_duration 180, default Random_Seed), the allowed smoothing/hysteresis ranges per metric, and validation helpers used by both parser and Settings (built in sleep-replay-app)
    - Add the new error codes (`INVALID_MAPPING_CONFIG`, `INVALID_RANDOM_SEED`, `NO_USABLE_DATA`, `RENDERING_FAILED`, `REPLAY_WRITE_FAILED`, `GENERATION_IN_PROGRESS`) to the domain error table
    - _Requirements: 1.2, 1.3, 1.4, 1.5_
  - [ ] 1.3 Implement the Replay_Manifest model
    - `backend/domain/replay.py`: frozen `Night_Event_Record`, `Metric_Availability`, `Coarse_State` segment triple, environmental window triple, and `Replay_Manifest` with every field of Requirement 10 criterion 1
    - _Requirements: 10.1, 10.5_
  - [ ] 1.4 Write unit tests for the domain vocabulary
    - Trajectory evaluation (endpoints, interior, clamping), default config completeness (all six keys, defaults applied), error-code table contains the new codes
    - _Requirements: 1.5, 6.6_

- [ ] 2. Implement the Mapping_Config_Parser
  - [ ] 2.1 Implement parsing and validation
    - `backend/sonification/config_parser.py`: UTF-8, 64 KB limit; JSON first, YAML (pinned PyYAML `SafeLoader` with duplicate-key detection) fallback; case-sensitive metric keys and targets; strict types (booleans and quoted numbers rejected where numbers are expected, non-integers where integers are expected); per-value default filling including empty documents; smoothing/hysteresis accepted only for `heart_rate`, `hrv`, `temperature`, `humidity`, `pressure`; `INVALID_MAPPING_CONFIG` with the 1-based line number for syntax errors or the full key path for key/value errors, listing allowed keys/targets/ranges/size limit
    - A rejected document applies no values and leaves the previously applied config unchanged
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7_
  - [ ] 2.2 Write unit tests for the parser
    - Every accepted and rejected key/type/range; empty YAML and empty JSON; duplicate keys; misplaced smoothing/hysteresis under `movement`; 64 KB boundary; line-number vs key-path error content; rejection atomicity
    - _Requirements: 1.1–1.7_

- [ ] 3. Implement the Mapping_Config_Printer
  - [ ] 3.1 Implement canonical YAML printing
    - `backend/sonification/config_printer.py`: all six metric keys with every accepted field (defaults applied), `target_duration`, `random_seed`, fixed key order, fixed number formatting, LF newlines; equivalent configs produce byte-identical documents
    - _Requirements: 1.8_
  - [ ] 3.2 Write property test for the config round trip
    - **Property 1: Mapping_Config parse-print-parse round trip**
    - **Validates: Requirements 1.9**
    - Hypothesis, ≥100 examples, tag `# Feature: sleep-replay-sonification, Property 1: ...`
  - [ ] 3.3 Write property test for format equivalence
    - **Property 2: Mapping_Config format equivalence**
    - **Validates: Requirements 1.10**
    - ≥100 examples: emit the same config as JSON and as YAML, parse both, assert equivalence

- [ ] 4. Implement Soundscape_Presets
  - [ ] 4.1 Implement preset definitions and per-window assignment
    - `backend/sonification/presets.py`: preset parameter sets (pad voicing/harmonics, texture color/density scale, modulation rate/depth, note behavior) for Awake, Light, Deep, REM, Neutral; assignment from Coarse_State (awake→Awake, light/asleep/restless→Light, deep→Deep, rem→REM, unknown→Neutral); restless texture_density boost = Restless_Texture_Boost × movement sensitivity, clamped to 1.0, movement sensitivity counted as 0.0 when its target is `none`, applied once per window
    - Preset plan with boundary-centered crossfades of min(Stage_Crossfade_Duration, shorter adjacent equal-preset run length), monotone gain exchange
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 2.8, 2.12_
  - [ ] 4.2 Write unit tests for preset assignment
    - Every Coarse_State mapping; restless boost with movement target `none` and with sensitivity; single application of the boost; crossfade shortening on short runs
    - _Requirements: 2.1–2.8, 2.12_

- [ ] 5. Implement normalization and metric contributions
  - [ ] 5.1 Implement Normalization_Spans
    - `backend/sonification/normalize.py`: per-session spans from non-missing smoothed Feature_Series values with the environmental (2.0 °C / 10 pp / 3.0 hPa) and physiological (10 bpm / 10 ms) minimum spans; span bounds map to 0.0/1.0; independent of Display_Units
    - _Requirements: 3.1, 3.4, 5.4_
  - [ ] 5.2 Write property test for normalization linearity
    - **Property 8: Environmental normalization linearity**
    - **Validates: Requirements 5.4**
    - ≥100 examples
  - [ ] 5.3 Implement Mapped_Values, hysteresis, and glides
    - `backend/sonification/contributions.py`: `0.5 + sensitivity × (normalized − 0.5)` clamped to [0, 1]; Neutral_Value 0.5 for target `none`, sensitivity 0.0, missing, or unavailable metrics; pressure excursion scaled so |contribution − 0.5| ≤ 0.25; per-metric Hysteresis_Reference hold/update; missing→glide to Neutral_Value and return→glide to Mapped_Value (≥1 s, ≤2 s for environmental) starting at the window boundary, from the current (possibly mid-glide) value; first-window-missing holds Neutral_Value from 0; constant Session_HRV contribution; movement contribution from Movement_Intensity
    - _Requirements: 3.5, 3.6, 3.7, 3.8, 5.3, 5.5, 5.6, 5.7, 6.4, 6.5, 6.10_
  - [ ] 5.4 Write property test for physiological monotonicity
    - **Property 3: Physiological contribution monotonicity**
    - **Validates: Requirements 3.3**
    - ≥100 examples
  - [ ] 5.5 Write property test for environmental monotonicity
    - **Property 9: Environmental contribution monotonicity**
    - **Validates: Requirements 5.8**
    - ≥100 examples
  - [ ] 5.6 Write property test for the pressure cap
    - **Property 10: Pressure contribution cap**
    - **Validates: Requirements 5.3**
    - ≥100 examples over smoothed values × sensitivities
  - [ ] 5.7 Write property test for hysteresis hold and update
    - **Property 11: Hysteresis hold and update**
    - **Validates: Requirements 6.4, 6.5**
    - ≥100 examples over smoothed-value sequences
  - [ ] 5.8 Write property test for glide behavior
    - **Property 5: Missing-feature glide behavior**
    - **Validates: Requirements 3.7, 3.8, 5.5, 5.6, 5.7**
    - ≥100 examples over missing-mask sequences
  - [ ] 5.9 Write property test for sensitivity monotonicity
    - **Property 14: Sensitivity monotonicity**
    - **Validates: Requirements 6.11**
    - ≥100 examples

- [ ] 6. Implement combination, rate limits, and the engine
  - [ ] 6.1 Implement weighted combination and the rate-limit pass
    - `backend/sonification/combine.py`: sensitivity-weighted mean per shared Sound_Parameter (missing/unavailable at Neutral_Value with sensitivity weight); Neutral_Value when untargeted or all-zero weights; breakpoint trajectories with ramps at window centers plus explicit glide/restless segments; chronological slope-clamping pass enforcing each parameter's rate limit for every pair of Replay_Times
    - _Requirements: 6.6, 6.7, 6.8, 6.9_
  - [ ] 6.2 Write property test for the rate limit
    - **Property 12: Sound_Parameter rate limit**
    - **Validates: Requirements 6.6**
    - ≥100 examples
  - [ ] 6.3 Write property test for the bounds invariant
    - **Property 13: Sound_Parameter bounds invariant**
    - **Validates: Requirements 6.7**
    - ≥100 examples over generated configs × Feature_Series
  - [ ] 6.4 Implement the Sonification_Engine
    - `backend/sonification/engine.py`: pure `build_render_plan(features, coarse_states, night_events, config, target_duration, seed) -> Render_Plan` combining normalization, contributions, combination, preset plan, transients, and gestures; PRNG only from `random.Random(f"{seed}:engine")`; no telemetry, timeline, session, clock, or I/O parameters
    - _Requirements: 7.1, 9.2_
  - [ ] 6.5 Write property test for features-only determinism
    - **Property 15: Sonification from features only**
    - **Validates: Requirements 7.1**
    - ≥100 examples: two sessions differing in telemetry but identical in features/states/events produce identical plans

- [ ] 7. Implement transient scheduling and gestures
  - [ ] 7.1 Implement movement and Brief_Awakening transients
    - `backend/sonification/transients.py`: one transient per window above Movement_Transient_Threshold with onset inside the window span (none at/below threshold); amplitude = sensitivity × intensity clamped to (0, 1]; restless-period raise of texture_density and transient_density proportional to movement sensitivity; Brief_Awakening transients at (start − session_start) / Compression_Ratio when shorter than Minimum_State_Duration × Compression_Ratio, scheduled even at sensitivity 0 / target `none`; Max_Transients_Per_Second cap per 1-second interval, omitting lowest amplitude first, later onset first on ties
    - _Requirements: 4.1, 4.2, 4.4, 4.5, 4.6, 4.7, 4.8, 4.9_
  - [ ] 7.2 Write property test for transient amplitude monotonicity
    - **Property 6: Movement transient amplitude monotonicity**
    - **Validates: Requirements 4.2**
    - ≥100 examples
  - [ ] 7.3 Write property test for the transient density cap
    - **Property 7: Transient density cap**
    - **Validates: Requirements 4.5, 4.9**
    - ≥100 examples
  - [ ] 7.4 Implement onset and awakening gestures
    - `backend/sonification/gestures.py`: one gesture per sleep onset / awakening Night_Event, ≥2 s, containing the event Replay_Time, within [0, Target_Duration], attack and release ≥0.5 s, no Transient_Event; inward clamping near endpoints
    - _Requirements: 6.12_
  - [ ] 7.5 Write unit tests for transients and gestures
    - Threshold inclusion/exclusion; restless raise strictly above un-raised values; Brief_Awakening placement; sensitivity 0 / target `none`; cap tie-breaking; gesture duration, containment, and endpoint clamping
    - _Requirements: 4.1, 4.4, 4.5, 4.6, 4.7, 4.8, 4.9, 6.12_

- [ ] 8. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 9. Implement the Audio_Renderer
  - [ ] 9.1 Implement layers, envelopes, and scale/key selection
    - `backend/audio/layers.py`: pad (additive scale-tone chords), pulse (soft sine pulses), texture (filtered noise), transient (enveloped blips, attack ≥5 ms, duration 50 ms–1.5 s); `backend/audio/envelopes.py`: attack ≥5 ms / release ≥20 ms helpers; `backend/audio/scales.py`: one pentatonic-major scale and one key per Replay derived from the Random_Seed, every note in-scale
    - Per-layer PRNG sub-streams `random.Random(f"{seed}:<layer>")` created at render start
    - _Requirements: 4.3, 6.1, 6.3, 8.4, 9.2_
  - [ ] 9.2 Implement the renderer core
    - `backend/audio/renderer.py`: block-wise rendering of the Render_Plan into a float64 stereo buffer; per-sample evaluation of breakpoint trajectories; Sound_Parameter → layer mapping (pulse_rate→pulse rate, rhythmic_density→pulse subdivisions, intensity→pad+texture gain, transient_density→texture grain rate, brightness→pad harmonics, texture_density→texture density, modulation→modulation depth); pulse rate 0.0→0.5/s, 1.0→2.0/s, non-decreasing; preset crossfade gain ramps; gestures; fade-in (0→2 s) and fade-out (last 3 s); exactly Target_Duration × 44,100 frames; post-conditions: finite samples, |Δsample| < 0.3 full scale, first/last samples ≤0.001
    - _Requirements: 3.2, 6.1, 6.12, 7.2, 7.3, 8.2, 8.3, 8.8, 8.9_
  - [ ] 9.3 Write property test for pulse rate bounds and monotonicity
    - **Property 4: Pulse rate bounds and monotonicity**
    - **Validates: Requirements 3.2**
    - ≥100 examples over pulse_rate values
  - [ ] 9.4 Implement loudness scaling, quantization, and the WAV writer
    - `backend/audio/loudness.py`: overall RMS/peak measurement, one global gain targeting −22 dBFS with peak limiting at 0.891, post-assertions (peak ≤ 0.891, RMS ∈ [−28, −16] dBFS, per-second RMS ≥ −50 dBFS in [2 s, end − 3 s]), clamp + fixed round-half-away-from-zero int16 quantization
    - `backend/audio/wav.py`: deterministic 44,100 Hz / 16-bit / stereo PCM writer (stdlib `wave` + `struct`)
    - _Requirements: 8.1, 8.5, 8.6, 8.7_
  - [ ] 9.5 Write property test for the WAV frame count
    - **Property 16: WAV frame count**
    - **Validates: Requirements 7.3, 8.2**
    - ≥5 examples across valid Target_Durations
  - [ ] 9.6 Write property test for audio output bounds
    - **Property 17: Audio output bounds**
    - **Validates: Requirements 8.1, 8.3, 8.5, 8.6, 8.7, 8.8, 8.9**
    - ≥5 rendered Replays from generated Render_Plans
  - [ ] 9.7 Write Preset_Comparison_Render tests
    - A helper rendering 120 s with one preset, all parameters 0.5, no transients/gestures, default seed; assert Deep vs Light (Spectral_Centroid ≤0.8×, Low_Frequency_Share ≥+0.10, lower modulation rate), REM modulation depth ≥ +0.2 over all presets, Awake note behavior (≥3 onsets, density ≤0.5× Light, onset-interval CV ≥0.3), Neutral between Light and Deep inclusive on all three measures
    - _Requirements: 2.9, 2.10, 2.11, 2.13_

- [ ] 10. Implement the Manifest_Serializer
  - [ ] 10.1 Implement canonical serialization and parsing
    - `backend/sonification/manifest.py`: canonical JSON (sorted keys, fixed separators, `ensure_ascii=False`, exact float round-trip, LF); ISO 8601 timestamps with seconds and the Display_Timezone offset; Replay_Times/durations in seconds; parse-time validation rejecting invalid JSON, missing fields, magnitudes outside [0, 1], Replay_Times outside [0, Target_Duration], and malformed Coarse_State segments, identifying at least one missing/invalid field and returning no manifest
    - _Requirements: 10.2, 10.3, 10.7, 10.8, 10.9_
  - [ ] 10.2 Write property test for the manifest round trip
    - **Property 19: Replay_Manifest serializer round trip**
    - **Validates: Requirements 10.4**
    - ≥100 examples
  - [ ] 10.3 Write unit tests for manifest content and rejection
    - Every field of criterion 1; no telemetry samples; timestamps and units; Coarse_State gap-free coverage; each rejection case
    - _Requirements: 10.1, 10.5, 10.7, 10.8, 10.9_

- [ ] 11. Implement generation orchestration, storage, and graceful degradation
  - [ ] 11.1 Implement generate_replay
    - `backend/sonification/generation.py`: seed precedence (CLI > config > default) with `INVALID_RANDOM_SEED` range validation; unusable-session check returning `NO_USABLE_DATA` naming Fitbit sleep/heart-rate files or a SensorPush CSV with no WAV and no Replay record; engine → renderer → loudness → manifest assembly (environmental window means, per-metric availability with missing fractions, Unavailable_Metric list, warnings); degradation rules (Unavailable_Metrics at Neutral_Value; no Stage_Data → Neutral preset throughout + warning; Fitbit-only and SensorPush-only warnings); non-finite sample abort with `RENDERING_FAILED` before any write
    - _Requirements: 9.3, 9.9, 11.1, 11.2, 11.3, 11.4, 11.5, 11.6, 11.7, 11.8, 8.13_
  - [ ] 11.2 Implement Replay storage
    - In `generation.py`: atomic WAV + manifest writes inside the Data_Directory before reporting completion; write failure removes partial files, leaves earlier Replays unchanged, returns `REPLAY_WRITE_FAILED` naming the Data_Directory; Replay record (paths, generation time, Input_Fingerprint, version, effective Mapping_Config) in the Metadata_Store; stored Mapping_Config reloads equivalent to the one used
    - _Requirements: 1.11, 8.12, 8.14, 10.6_
  - [ ] 11.3 Write property test for graceful degradation loudness
    - **Property 20: Graceful degradation loudness**
    - **Validates: Requirements 11.1**
    - ≥5 examples over generated sessions with random unavailable-metric subsets (30 s Target_Duration)
  - [ ] 11.4 Write unit tests for degradation and storage
    - `NO_USABLE_DATA` content and no-record assertion; Unavailable_Metric listing; Neutral-preset, Fitbit-absent, and SensorPush-absent warnings; write-failure cleanup; stored-config equivalence
    - _Requirements: 1.11, 8.14, 10.6, 11.2, 11.4, 11.6, 11.7, 11.8_

- [ ] 12. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 13. Implement determinism guarantees
  - [ ] 13.1 Write property test for determinism and confluence
    - **Property 18: Replay determinism and confluence**
    - **Validates: Requirements 9.1, 9.4, 9.5, 9.6**
    - ≥5 examples: two generations from identical inputs (including permuted file order) → byte-identical WAV and manifest; differing only in seed → manifests identical except seed fields, WAV same format/length with ≥1 differing sample
  - [ ] 13.2 Write cross-process and environment independence tests
    - Render in a fresh process after importing into an empty Data_Directory matches an in-suite render (9.8); outputs unchanged across locale, host timezone with explicit Display_Timezone, output path, Data_Directory location, and Display_Units (9.7)
    - _Requirements: 9.7, 9.8_

- [ ] 14. Implement the CLI
  - [ ] 14.1 Implement the sample-data command
    - `backend/api/cli.py`: `sample-data --out DIR [--seed N]`; creates DIR, replaces same-named files only, leaves other files untouched; without a seed the output is byte-identical to the committed `sample_data/`
    - _Requirements: 12.1_
  - [ ] 14.2 Implement the generate command
    - In `cli.py`: paths (Fitbit files/one zip + SensorPush CSVs, at least one, no upload limit); options `--config`, `--target-duration`, `--seed`, per-file-type Source_Timezone overrides, `--session-date` or `--manual-start`/`--manual-end` (mutually exclusive; offset-less times in Display_Timezone), `--output` (manifest beside it, same stem, replacing existing); session selection per the Session_Detector defaults, session-date rules (latest main-sleep ending that date, else longest), and manual-range validation; setting precedence CLI > config file > defaults, Metadata_Store settings ignored; success prints absolute paths, session times with offset, Compression_Ratio to one decimal, event count, one warning per line, exit 0; failures print code/description/file/action/reference-id to stderr, leave outputs unchanged, exit non-zero; no-candidate-on-date error lists available end dates
    - _Requirements: 12.2, 12.3, 12.4, 12.5, 12.6, 12.7, 12.8_
  - [ ] 14.3 Write CLI tests
    - Sample-data byte-identity and directory behavior; argument validation; precedence; session-date selection and error listing end dates; manual range; stdout/stderr contract and exit codes
    - _Requirements: 12.1–12.8_

- [ ] 15. End-to-end, performance, and example Replay
  - [ ] 15.1 Write the end-to-end generation test
    - Import the Sample_Dataset, generate the default-selected session with Default_Mapping, default seed, 3 min: format, duration within 50 ms, loudness limits, manifest parses, 1–12 Night_Events
    - _Requirements: 14.3_
  - [ ] 15.2 Write the determinism test with different seed
    - Two identical 30 s generations → byte-identical WAV and manifest; third with a different seed → differing samples, identical Coarse_State segments and Night_Events
    - _Requirements: 14.4_
  - [ ] 15.3 Write performance tests
    - 30 s / 2 min / 3 min Replays stored within 60 s; 5 min / 10 min within 180 s on the Reference_Machine (marked so they can be skipped off-reference hardware)
    - _Requirements: 8.10, 8.11_
  - [ ] 15.4 Generate and commit the example Replay
    - `examples/example_replay.wav` + manifest from the Sample_Dataset with the Default_Mapping; document the regenerating command with its Target_Duration, Random_Seed, and Display_Timezone; a test asserts the documented command regenerates a byte-identical WAV
    - _Requirements: 13.1_

- [ ] 16. Final checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise; confirm `python -m pytest` finishes within 5 minutes.

## Notes

- Each property test uses Hypothesis with `@settings(max_examples=100)` (5 for the render-involving Properties 16, 17, 18, and 20), keeps its strategies local to its own test file, and carries the tag `# Feature: sleep-replay-sonification, Property N: <text>`.
- Render-heavy tests use the 30 s Target_Duration where the duration is not under test, to keep the suite within 5 minutes on the Reference_Machine (Requirement 14.4 of sleep-replay-app applies to the combined suite).
- The performance tests (8.10/8.11) are Reference_Machine-timed; mark them so contributors on slower hardware can deselect them without failing the suite.
- The Frontend-facing parts referenced by this spec's requirements (Main_Screen Generate button, Settings_Panel values) are built in sleep-replay-app; this spec implements the generation behavior they call.
- Documentation deliverables (mapping-config reference, CLI reference, troubleshooting codes, example config file) are tracked in sleep-replay-app; the example Mapping_Config values and final parameter values documented there must equal `DEFAULT_MAPPING`.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1"] },
    { "id": 1, "tasks": ["1.2", "1.3"] },
    { "id": 2, "tasks": ["1.4", "2.1", "4.1", "5.1"] },
    { "id": 3, "tasks": ["2.2", "3.1", "4.2", "5.2", "5.3", "9.1"] },
    { "id": 4, "tasks": ["3.2", "3.3", "5.4", "5.5", "5.6", "5.7", "5.8", "5.9", "7.1", "7.4", "9.2"] },
    { "id": 5, "tasks": ["6.1", "7.2", "7.3", "7.5", "9.3", "9.4"] },
    { "id": 6, "tasks": ["6.2", "6.3", "6.4", "9.5", "9.6", "9.7", "10.1"] },
    { "id": 7, "tasks": ["6.5", "10.2", "10.3"] },
    { "id": 8, "tasks": ["11.1"] },
    { "id": 9, "tasks": ["11.2"] },
    { "id": 10, "tasks": ["11.3", "11.4", "13.1", "14.1", "14.2"] },
    { "id": 11, "tasks": ["13.2", "14.3", "15.1", "15.2", "15.3"] },
    { "id": 12, "tasks": ["15.4"] }
  ]
}
```
