# Requirements Document

## Introduction

Sleep Replay is an open-source, local-first application that turns one night of sleep and bedroom-environment telemetry into a short ambient audio composition (30 seconds to 10 minutes, default 3 minutes). After waking, the user imports a Fitbit export and a SensorPush CSV. The application discovers the night's sleep session, aligns the datasets on a common timeline, extracts features and events, maps them to a small set of interacting musical dimensions, and renders a WAV file. Sleep Replay is not a medical device, not a diagnostic tool, and not an alarm.

Example: 8 hours of telemetry → normalize and align → extract features and events over time windows → map to sound parameters → render ~3 minutes of new audio (160x temporal compression).

This spec, sleep-replay-sonification, is the second of three specs split from the Sleep Replay MVP requirements. It builds on sleep-replay-data-pipeline, which provides the Aligned_Timeline, Feature_Series, Coarse_States, and Night_Events, and turns them into a coherent ambient soundscape rendered to a WAV file with a Replay_Manifest, reachable through the CLI. It covers the Mapping_Config, the Soundscape_Presets, the heart rate, HRV, movement, and environmental sound mappings, parameter smoothing, audio rendering and output, determinism, the Replay_Manifest, graceful degradation, Replay generation, the CLI, and the example Replay. Its result is a playable WAV file and Replay_Manifest generated from the Sample_Dataset through the CLI. The spec sleep-replay-app adds the Backend_API and the Frontend later.

The guiding audio principle is one coherent ambient composition: every metric shapes a small set of interacting musical dimensions of shared Sound_Layers, so that the replay sounds intentional instead of a series of beeps for each sensor change.

## Related Specs

Terms not defined in this document are defined in the Glossary of sleep-replay-data-pipeline, which also holds the Input Format Assumptions and the Technology and Environment Constraints.

#[[file:.kiro/specs/sleep-replay-data-pipeline/requirements.md]]

## Scope

### In Scope (this spec)

- Mapping_Config parsing, validation, and printing (Requirement 1).
- Soundscape_Presets per Coarse_State (Requirement 2).
- Heart rate, HRV, movement, and environmental sound mappings (Requirements 3 to 5).
- Coherent soundscape, hysteresis, and parameter smoothing (Requirement 6).
- Sonification from features only, and the night compression criteria on Sonification_Engine inputs, on excluding raw telemetry from audio, and on the WAV frame count (Requirement 7).
- Audio continuity, loudness, and WAV output (Requirement 8).
- Determinism of Replays (Requirement 9).
- Replay_Manifest and Manifest_Serializer (Requirement 10).
- Graceful degradation when data sources or metrics are absent (Requirement 11).
- CLI for sample data and Replay generation (Requirement 12).
- Example Replay WAV file generated from the Sample_Dataset (Requirement 13).
- Automated tests for sonification and audio (Requirement 14).

### Out of Scope (this spec)

- Specified in sleep-replay-data-pipeline: the domain model, Fitbit and SensorPush import, the Data_Source_Adapter interface, sleep session discovery and selection, timezone handling, alignment and resampling, the night compression time mapping and Target_Duration validation, feature extraction and Coarse_States, Night_Event detection, the Sample_Dataset and Sample_Data_Generator, the Data_Directory and local persistence foundation, User_Error structure and report warnings, and the architecture and Dependency_Rules.
- Specified in sleep-replay-app: the Backend_API; the Import_View, Main_Screen, Playback_Timeline, and Settings_Panel; the complete end-to-end user flow through the Frontend; privacy and network restrictions; setup and Docker; the remaining Documentation deliverables.
- Where a criterion of this spec names the Backend_API, Frontend, Main_Screen, or Settings_Panel, this spec specifies the sonification and Replay generation behavior; the named interface is specified in sleep-replay-app.

### Non-Goals (MVP)

The product-wide Non-Goals are listed in sleep-replay-data-pipeline and apply unchanged to this spec.

## Default Parameter Values

These are MVP starting points referenced by the requirements. Implementation may tune them for sound quality; final values are recorded in the documentation and the example Mapping_Config.

| Parameter | Default |
|---|---|
| Hysteresis_Threshold | heart_rate 1 bpm; hrv_rmssd 2 ms; temperature 0.1 °C; humidity 0.5 percentage points; pressure 0.1 hPa |
| Environmental normalization minimum span | temperature 2.0 °C; humidity 10 percentage points; pressure 3.0 hPa |
| Physiological normalization minimum span | heart_rate 10 bpm; hrv_rmssd 10 ms |
| Stage_Crossfade_Duration | 2.0 s of Replay_Time |
| Pulse rate range | 0.5 to 2.0 pulses per second of Replay_Time |
| Max_Transients_Per_Second | 4 per second of Replay_Time |
| Parameter rate limits | fast parameters (pulse_rate, rhythmic_density, intensity, transient_density): 0.5 of range per second of Replay_Time; slow parameters (brightness, texture_density, modulation): 0.2 of range per second of Replay_Time |
| Pressure maximum contribution | 0.25 of the target Sound_Parameter range |
| Restless_Texture_Boost | 0.25 of the texture_density range |
| Audio format | WAV, 44,100 Hz, 16-bit PCM, 2 channels |
| Fade-in / fade-out | 2.0 s / 3.0 s |
| Loudness | peak ≤ −1 dBFS; overall RMS between −28 dBFS and −16 dBFS |
| Default Random_Seed | a fixed integer documented in the example Mapping_Config |

The remaining parameters (Timeline_Resolution candidates, Max_Interpolation_Gap, smoothing windows, Feature_Window_Span, Minimum_State_Duration, event detection thresholds, Max_Event_Count, upload and archive size limits, Display_Units) are defined in sleep-replay-data-pipeline.

## Glossary

This glossary adds the terms used by this spec that are not defined in the sleep-replay-data-pipeline Glossary.

- **Mapping_Config metric keys**: `heart_rate`, `hrv`, `movement`, `temperature`, `humidity`, `pressure`.
- **Unavailable_Metric**: A Mapping_Config metric key for which the selected SleepSession provides no source data: heart_rate has no heart_rate TelemetryPoint; hrv has no hrv_rmssd TelemetryPoint and no Session_HRV; movement has no steps TelemetryPoint and no Stage_Data; temperature, humidity, or pressure has no TelemetryPoint of that metric.
- **Mapping_Config_Parser**: The component that parses and validates Mapping_Config documents.
- **Mapping_Config_Printer**: The component that formats a Mapping_Config as a YAML document.
- **Default_Mapping**: heart_rate → pulse_rate (0.5); hrv → modulation (0.3); movement → transient_density (0.8); temperature → brightness (0.3); humidity → texture_density (0.3); pressure → modulation (0.2).
- **Sensitivity**: A per-metric value from 0.0 to 1.0 scaling the influence of the metric on its target Sound_Parameter.
- **Neutral_Value**: The value a metric's contribution takes when the metric is missing, unavailable, disabled, or has Sensitivity 0.0.
- **Normalization_Span**: For an environmental metric, the interval in the metric's Canonical_Unit centered on the midpoint between the minimum and maximum smoothed Feature_Series values not marked missing within the SleepSession, with a width equal to the larger of (maximum − minimum) and the metric's environmental normalization minimum span.
- **Mapped_Value**: The contribution a metric's smoothed value produces on its target Sound_Parameter before missing-data glides, Hysteresis_Threshold holding, and parameter rate limits are applied.
- **Hysteresis_Reference**: The smoothed value of a metric, in the metric's Canonical_Unit, at which the metric's contribution was last updated; initially the first non-missing smoothed value of the metric in the SleepSession.
- **Soundscape_Preset**: The sonic character applied for a Coarse_State: Awake, Light, Deep, REM, or Neutral.
- **Preset_Comparison_Render**: A 120 s test render by the Audio_Renderer that applies one Soundscape_Preset throughout, holds every Sound_Parameter at 0.5, schedules no Transient_Events and no sleep onset or awakening gestures, and uses the default Random_Seed; measured over an analysis interval from 5 s to 115 s of Replay_Time.
- **Spectral_Centroid**: The magnitude-weighted mean frequency, in Hz, of the spectrum of the mono sum of both channels over the analysis interval.
- **Low_Frequency_Share**: The fraction (0.0 to 1.0) of the spectral energy of the mono sum that lies below 200 Hz over the analysis interval.
- **Note_Event_Density**: The number of note onsets in the pad and texture Sound_Layers within the analysis interval, divided by the interval length in seconds.
- **Preset_Modulation_Rate**: The rate in Hz of the periodic modulation a Soundscape_Preset applies to the pad and texture Sound_Layers.
- **Preset_Modulation_Depth**: The depth, from 0.0 to 1.0, of the periodic modulation a Soundscape_Preset applies to the pad and texture Sound_Layers.
- **Restless_Texture_Boost**: The largest texture_density increase applied while the Coarse_State is restless.
- **Sound_Layer**: One of the fixed voices of the soundscape: pad, pulse, texture, transient.
- **Note_Event**: A pitched sound with a defined start and end time rendered on the pad or pulse Sound_Layer.
- **Manifest_Serializer**: The component that serializes Replay_Manifests to JSON and parses JSON back into Replay_Manifests.
- **Generation_Inputs**: The complete set of inputs that determine a Replay: the input file contents, the Source_Timezone overrides, the selected SleepSession, the Display_Timezone, the Mapping_Config (including Target_Duration), and the Random_Seed.

## Requirements

### Requirement 1: Mapping Configuration

**User Story:** As a user, I want a simple YAML or JSON file that controls how my data maps to sound, so that I can experiment with different renditions of the same night.

#### Acceptance Criteria

1. THE Mapping_Config_Parser SHALL parse UTF-8 encoded Mapping_Config documents of up to 64 KB in YAML format and in JSON format.
2. THE Mapping_Config_Parser SHALL accept, for each Mapping_Config metric key, a target equal to one of `pulse_rate`, `rhythmic_density`, `intensity`, `transient_density`, `brightness`, `texture_density`, `modulation`, or `none`, and a Sensitivity given as a number from 0.0 to 1.0 inclusive, matching metric keys and target names case-sensitively.
3. THE Mapping_Config_Parser SHALL accept, for the metric keys `heart_rate`, `hrv`, `temperature`, `humidity`, and `pressure` only, an optional smoothing window in whole minutes of Night_Time from 1 to 60 and an optional Hysteresis_Threshold in the metric's Canonical_Unit within these ranges: heart_rate 0 to 10 bpm; hrv 0 to 20 ms; temperature 0 to 1.0 °C; humidity 0 to 5 percentage points; pressure 0 to 1.0 hPa.
4. THE Mapping_Config_Parser SHALL accept an optional global target_duration given as an integer number of seconds equal to 30, 120, 180, 300, or 600, and an optional global random_seed given as an integer from 0 to 4,294,967,295.
5. WHEN the Mapping_Config_Parser parses a document that omits a metric key, a metric's target or Sensitivity, or an optional value, including an empty YAML document or an empty JSON object, THE Mapping_Config_Parser SHALL fill each omitted value individually with its default: the Default_Mapping target and Sensitivity of the metric, the smoothing window and Hysteresis_Threshold from the Default Parameter Values, a target_duration of 180 seconds, and the default Random_Seed.
6. IF a Mapping_Config document exceeds 64 KB, has invalid YAML or JSON syntax, contains a duplicate key, an unknown or misplaced key (including a smoothing window or Hysteresis_Threshold under `movement`), an unknown target, a value of the wrong type (including a boolean or quoted number where a number is expected, or a non-integer where an integer is expected), or a non-finite or out-of-range value, THEN THE Mapping_Config_Parser SHALL return a User_Error that states the 1-based line number for syntax errors or the full key path of the offending entry for key and value errors, and lists the allowed keys, targets, value range, or size limit.
7. IF the Mapping_Config_Parser rejects a Mapping_Config document, THEN THE Backend SHALL apply none of the document's values, keep the previously applied Mapping_Config unchanged, and generate no Replay from the rejected document.
8. THE Mapping_Config_Printer SHALL format any valid Mapping_Config as a YAML document that contains all six Mapping_Config metric keys, every value the Mapping_Config_Parser accepts for each key, target_duration, and random_seed, including values filled from defaults, in a fixed key order so that equivalent Mapping_Configs produce byte-identical documents.
9. FOR ALL valid Mapping_Config documents in YAML or JSON format, parsing then printing then parsing SHALL produce an equivalent Mapping_Config, where equivalent means numerically equal target, Sensitivity, smoothing window, and Hysteresis_Threshold for every metric key and equal target_duration and random_seed (round-trip property).
10. FOR ALL valid Mapping_Configs, parsing a JSON document and a YAML document that express the same keys and values SHALL produce equivalent Mapping_Configs (format-equivalence property).
11. WHEN the Backend generates a Replay, THE Backend SHALL store the Mapping_Config used for that Replay, with all defaults applied, in the Metadata_Store linked to the Replay record, such that loading the stored Mapping_Config yields a Mapping_Config equivalent to the one used for generation.

### Requirement 2: Sleep Stage Soundscape Character

**User Story:** As a listener, I want each part of the night to have its own broad sonic character, so that I can hear the shape of my sleep.

#### Acceptance Criteria

1. THE Sonification_Engine SHALL assign exactly one Soundscape_Preset to each Feature_Window, determined only by the Coarse_State of that Feature_Window as specified in criteria 2 to 8.
2. WHILE the Coarse_State is awake, THE Sonification_Engine SHALL apply the Awake preset.
3. WHILE the Coarse_State is light, THE Sonification_Engine SHALL apply the Light preset.
4. WHILE the Coarse_State is deep, THE Sonification_Engine SHALL apply the Deep preset.
5. WHILE the Coarse_State is rem, THE Sonification_Engine SHALL apply the REM preset.
6. WHILE the Coarse_State is asleep, THE Sonification_Engine SHALL apply the Light preset.
7. WHILE the Coarse_State is restless, THE Sonification_Engine SHALL apply the Light preset and raise texture_density by the Restless_Texture_Boost multiplied by movement Sensitivity, clamped to 1.0, counting movement Sensitivity as 0.0 when the movement target is none, and SHALL apply the texture_density increase only once where Requirement 4 criterion 4 also applies to the same Feature_Window.
8. WHILE the Coarse_State is unknown, THE Sonification_Engine SHALL apply the Neutral preset.
9. THE Audio_Renderer SHALL render the Deep preset with a Spectral_Centroid at most 0.8 times the Light preset's, a Low_Frequency_Share at least 0.10 higher than the Light preset's, and a Preset_Modulation_Rate lower than the Light preset's, each measured on Preset_Comparison_Renders.
10. THE Audio_Renderer SHALL render the REM preset with a Preset_Modulation_Depth at least 0.2 greater than that of every other Soundscape_Preset, measured on Preset_Comparison_Renders.
11. THE Audio_Renderer SHALL render the Awake preset with at least 3 note onsets in the analysis interval, a Note_Event_Density at most 0.5 times the Light preset's, and a coefficient of variation (standard deviation divided by mean) of the intervals between consecutive note onsets of at least 0.3, each measured on Preset_Comparison_Renders.
12. WHEN the Soundscape_Preset changes between two consecutive Feature_Windows, THE Audio_Renderer SHALL crossfade from the outgoing to the incoming Soundscape_Preset, centered on the boundary between the two Feature_Windows, lasting the Stage_Crossfade_Duration or the Replay_Time length of the shorter adjacent run of equal Soundscape_Preset, whichever is shorter, with the outgoing preset's gain decreasing monotonically from full to zero and the incoming preset's gain increasing monotonically from zero to full.
13. THE Audio_Renderer SHALL render the Neutral preset so that its Spectral_Centroid, Low_Frequency_Share, and Preset_Modulation_Rate each lie between the corresponding Light and Deep preset values (inclusive), measured on Preset_Comparison_Renders.

### Requirement 3: Heart Rate and HRV Mapping

**User Story:** As a listener, I want my heart rate and HRV to shape a subtle pulse and slow modulation, so that my body's rhythm is present without a literal heartbeat track.

#### Acceptance Criteria

1. THE Sonification_Engine SHALL derive the heart_rate contribution to the heart_rate mapping target (default pulse_rate) for each Feature_Window from the Feature_Window's smoothed heart rate, normalized relative to the SleepSession range of smoothed heart rate using a span no smaller than the heart_rate physiological normalization minimum span.
2. THE Audio_Renderer SHALL derive the pulse event rate from the pulse_rate Sound_Parameter so that pulse_rate 0.0 produces the lower bound of the pulse rate range, pulse_rate 1.0 produces the upper bound of the pulse rate range, and every intermediate pulse_rate value produces a rate within the pulse rate range that is non-decreasing in pulse_rate, independent of the measured heart rate in bpm.
3. FOR ALL pairs of smoothed values v1 ≤ v2 of the same metric (heart rate or hrv_rmssd) within a SleepSession whose corresponding Sensitivity (heart_rate or hrv) is above 0.0, the contribution computed for v1 SHALL be less than or equal to the contribution computed for v2, evaluated before hysteresis, rate limiting, and combination with other metrics (monotonicity property).
4. THE Sonification_Engine SHALL derive the hrv contribution to the hrv mapping target (default modulation) for each Feature_Window from the Feature_Window's smoothed hrv_rmssd, normalized relative to the SleepSession range of smoothed hrv_rmssd using a span no smaller than the hrv_rmssd physiological normalization minimum span.
5. IF the SleepSession has a Session_HRV and no hrv_rmssd TelemetryPoints within the SleepSession, THEN THE Sonification_Engine SHALL apply one constant hrv contribution to every Feature_Window of the Replay, where a greater Session_HRV produces an equal or greater contribution.
6. IF the SleepSession has no hrv_rmssd TelemetryPoints within the SleepSession and no Session_HRV, THEN THE Sonification_Engine SHALL hold the hrv contribution at the Neutral_Value for the entire Replay.
7. IF the heart_rate or hrv_rmssd features of a Feature_Window are marked missing and the features of the same metric in the preceding Feature_Window are not marked missing, THEN THE Sonification_Engine SHALL glide that metric's contribution to the Neutral_Value over at least 1 s of Replay_Time.
8. WHEN the heart_rate or hrv_rmssd features of a Feature_Window are not marked missing and the features of the same metric in the preceding Feature_Window are marked missing, THE Sonification_Engine SHALL glide that metric's contribution from the Neutral_Value to the mapped value over at least 1 s of Replay_Time.

### Requirement 4: Movement Mapping

**User Story:** As a listener, I want movement to appear as short, noticeable sonic events, so that restless moments stand out without dominating the soundscape.

#### Acceptance Criteria

1. WHERE the movement target is not none and movement Sensitivity is above 0.0, WHEN Movement_Intensity in a Feature_Window is greater than Movement_Transient_Threshold, THE Sonification_Engine SHALL schedule at least one Transient_Event triggered by movement with its onset within the Replay_Time span of that Feature_Window, unless the Transient_Event is omitted under the Max_Transients_Per_Second limit.
2. THE Sonification_Engine SHALL assign each Transient_Event triggered by movement an amplitude greater than 0.0 and at most 1.0 that is non-decreasing in Movement_Intensity for a fixed movement Sensitivity and non-decreasing in movement Sensitivity for a fixed Movement_Intensity (monotonicity property).
3. THE Audio_Renderer SHALL shape each Transient_Event with an attack of at least 5 ms and a total duration, measured from onset to the end of the release, between 50 ms and 1.5 s.
4. WHERE the movement target is not none and movement Sensitivity is above 0.0, WHILE a Feature_Window lies within a Restless_Period, THE Sonification_Engine SHALL raise texture_density and transient_density for that Feature_Window above the values computed without the raise, by an amount greater than 0.0 and proportional to movement Sensitivity, subject to the 0.0 to 1.0 Sound_Parameter range and the parameter rate limits.
5. THE Sonification_Engine SHALL schedule Transient_Events, counting those triggered by movement and those triggered by Brief_Awakenings, such that every 1-second interval of Replay_Time contains the onsets of at most Max_Transients_Per_Second Transient_Events.
6. WHEN a Brief_Awakening lasts less than Minimum_State_Duration multiplied by the Compression_Ratio, THE Sonification_Engine SHALL render the Brief_Awakening as one Transient_Event with its onset at the Replay_Time equal to the Brief_Awakening start_time minus the SleepSession start_time, divided by the Compression_Ratio, instead of a Coarse_State change.
7. IF Movement_Intensity in a Feature_Window is at or below Movement_Transient_Threshold, THEN THE Sonification_Engine SHALL schedule no Transient_Event triggered by movement with its onset in that Feature_Window.
8. IF movement Sensitivity is 0.0 or the movement target is none, THEN THE Sonification_Engine SHALL schedule no Transient_Events triggered by movement and apply no Restless_Period raise, while continuing to schedule Transient_Events for Brief_Awakenings.
9. IF the candidate Transient_Events with onsets in any 1-second interval of Replay_Time outnumber Max_Transients_Per_Second, THEN THE Sonification_Engine SHALL omit candidates in order of lowest amplitude first, omitting the later onset first when amplitudes are equal, until every 1-second interval satisfies the limit.

### Requirement 5: Environmental Mapping

**User Story:** As a listener, I want bedroom temperature, humidity, and pressure to shape slow tonal and textural changes, so that the room is part of the replay.

#### Acceptance Criteria

1. THE Sonification_Engine SHALL derive, for each Feature_Window, the temperature contribution to the target of the `temperature` Mapping_Config metric key (Default_Mapping: brightness) from the smoothed temperature value in the Feature_Series.
2. THE Sonification_Engine SHALL derive, for each Feature_Window, the humidity contribution to the target of the `humidity` Mapping_Config metric key (Default_Mapping: texture_density) from the smoothed humidity value in the Feature_Series.
3. THE Sonification_Engine SHALL derive, for each Feature_Window, the pressure contribution to the target of the `pressure` Mapping_Config metric key (Default_Mapping: modulation) from the smoothed pressure value in the Feature_Series, keeping the absolute difference between the pressure contribution and the Neutral_Value at or below the pressure maximum contribution (0.25) at every Replay_Time for every pressure Sensitivity from 0.0 to 1.0.
4. THE Sonification_Engine SHALL normalize each smoothed temperature, humidity, and pressure value in its Canonical_Unit, independent of the Display_Units, onto the range 0.0 to 1.0 over the metric's Normalization_Span, such that the lower and upper bounds of the Normalization_Span map to 0.0 and 1.0 and a difference of Δ between two smoothed values produces a normalized difference of Δ divided by the Normalization_Span width.
5. WHEN an environmental metric's features become marked missing in a Feature_Window that follows a Feature_Window with non-missing features, THE Sonification_Engine SHALL glide the metric's contribution monotonically from its current value to the Neutral_Value, starting at the beginning of the missing Feature_Window and lasting at least 1 s and at most 2 s of Replay_Time, and then hold the contribution at the Neutral_Value while the metric's features remain marked missing.
6. WHEN an environmental metric's features become non-missing in a Feature_Window that follows one or more Feature_Windows marked missing, THE Sonification_Engine SHALL glide the metric's contribution from its current value (including a value reached during an unfinished glide toward the Neutral_Value) to the Mapped_Value, starting at the beginning of that Feature_Window and lasting at least 1 s and at most 2 s of Replay_Time.
7. IF an environmental metric's features are marked missing in the first Feature_Window of the Replay, THEN THE Sonification_Engine SHALL hold the metric's contribution at the Neutral_Value from Replay_Time 0 until the first Feature_Window with non-missing features for that metric.
8. FOR ALL pairs of smoothed values v1 ≤ v2 of temperature, humidity, or pressure within a SleepSession where the metric's Sensitivity is above 0.0, the Mapped_Value for v1 SHALL be less than or equal to the Mapped_Value for v2 (monotonicity property).

### Requirement 6: Coherent Soundscape and Parameter Smoothing

**User Story:** As a listener, I want one coherent ambient composition, so that the replay sounds intentional instead of a series of beeps for each sensor change.

#### Acceptance Criteria

1. THE Audio_Renderer SHALL produce every sound of a Replay from the four Sound_Layers pad, pulse, texture, and transient, with no additional voice or Sound_Layer at any Replay_Time, including during Soundscape_Preset crossfades.
2. THE Sonification_Engine SHALL express heart rate, HRV, temperature, humidity, and pressure exclusively as changes to Sound_Parameters of the shared Sound_Layers, so that no value, value change, or environmental Night_Event of these metrics triggers a Transient_Event.
3. THE Audio_Renderer SHALL select exactly one musical scale and one key for each Replay, keep both unchanged from Replay_Time 0 to the Target_Duration, and assign every pitched note a nominal pitch belonging to that scale and key.
4. IF the absolute difference between a smoothed metric's current value and the metric's Hysteresis_Reference is less than the metric's Hysteresis_Threshold, THEN THE Sonification_Engine SHALL hold the metric's contribution at the previously applied value.
5. WHEN the absolute difference between a smoothed metric's current value and the metric's Hysteresis_Reference reaches or exceeds the metric's Hysteresis_Threshold, THE Sonification_Engine SHALL update the metric's contribution from the current smoothed value and set the Hysteresis_Reference to the current smoothed value.
6. THE Sonification_Engine SHALL limit each Sound_Parameter trajectory so that, for any two Replay_Times t1 < t2, the absolute change of the Sound_Parameter between t1 and t2 does not exceed the parameter's rate limit from the parameter rate limits multiplied by (t2 − t1), whether the change results from metric contributions, Coarse_State changes, restless periods, or glides to or from the Neutral_Value.
7. FOR ALL valid Mapping_Configs and SleepSessions, every Sound_Parameter value at every Replay_Time SHALL lie within 0.0 to 1.0 inclusive (invariant).
8. IF two or more metrics target the same Sound_Parameter, THEN THE Sonification_Engine SHALL set the combined metric contribution to that Sound_Parameter to the mean of the metrics' contributions weighted by their Sensitivities, counting a missing or unavailable metric at the Neutral_Value with its Sensitivity as weight.
9. IF no metric targets a Sound_Parameter, or the Sensitivities of all metrics targeting it sum to 0.0, THEN THE Sonification_Engine SHALL set the combined metric contribution to that Sound_Parameter to the Neutral_Value.
10. IF a metric's target is none or the metric's Sensitivity is 0.0, THEN THE Sonification_Engine SHALL hold the metric's contribution at the Neutral_Value of 0.5, so that changing the metric's values leaves every Sound_Parameter trajectory unchanged.
11. FOR ALL fixed metric values and Sensitivities s1 ≤ s2, the absolute deviation of the metric's contribution from the Neutral_Value at s2 SHALL be greater than or equal to the absolute deviation at s1 (monotonicity property).
12. THE Audio_Renderer SHALL render each sleep onset and awakening Night_Event as a gesture that lasts at least 2 s of Replay_Time, contains the Night_Event's Replay_Time, lies entirely between 0 and the Target_Duration, has an amplitude envelope with an attack and a release of at least 0.5 s each, and triggers no Transient_Event.

### Requirement 7: Sonification from Features

**User Story:** As a user, I want to choose how long my replay is, so that a whole night fits into a short listening session.

#### Acceptance Criteria

1. THE Sonification_Engine SHALL derive every Sound_Parameter trajectory and every Transient_Event exclusively from Feature_Series, Coarse_States, Night_Events, the Mapping_Config, the Target_Duration, and the Random_Seed, so that two SleepSessions with identical Feature_Series, Coarse_States, and Night_Events produce identical Sound_Parameter trajectories and Transient_Events under the same Mapping_Config, Target_Duration, and Random_Seed.
2. THE Audio_Renderer SHALL synthesize Replay audio exclusively from Sound_Parameter trajectories and Transient_Events, and SHALL NOT time-scale, resample, or play back raw telemetry values as audio signals.
3. WHEN the Audio_Renderer completes a Replay, THE Audio_Renderer SHALL produce a WAV file containing exactly Target_Duration (in seconds) × 44,100 sample frames (for example, 7,938,000 frames for 3 minutes), with the fade-in and fade-out contained within that length.

### Requirement 8: Audio Continuity and Output

**User Story:** As a listener, I want smooth, click-free audio at a comfortable level, so that the replay is pleasant to listen to right after waking.

#### Acceptance Criteria

1. THE Audio_Renderer SHALL write each Replay as a WAV file with a sample rate of 44,100 Hz, 16-bit PCM encoding, and 2 channels.
2. THE Audio_Renderer SHALL produce Replay audio whose duration, computed as the WAV frame count divided by 44,100, differs from the Target_Duration by at most 50 ms.
3. THE Audio_Renderer SHALL apply a fade-in whose gain rises monotonically from 0.0 at Replay_Time 0 s to 1.0 at Replay_Time 2.0 s, and a fade-out whose gain falls monotonically from 1.0 at 3.0 s before the last sample to 0.0 at the last sample, so that the first and last samples of each channel have an absolute value of at most 0.001 of full scale.
4. THE Audio_Renderer SHALL shape every Note_Event and Transient_Event with an amplitude envelope that rises from zero to its peak amplitude over at least 5 ms (attack) and falls from its final amplitude to zero over at least 20 ms (release).
5. THE Audio_Renderer SHALL keep the absolute value of every sample in each channel of each Replay at or below −1 dBFS (0.891 of full scale).
6. THE Audio_Renderer SHALL scale each Replay so that the overall RMS level, computed as 20·log10(RMS / full scale) over all samples of both channels across the full Replay including the fade-in and fade-out, lies between −28 dBFS and −16 dBFS inclusive while criterion 5 also holds.
7. THE Audio_Renderer SHALL keep the RMS level, computed as in criterion 6 over both channels, at or above −50 dBFS in every non-overlapping 1-second window that starts at a whole second of Replay_Time at or after 2.0 s and ends at or before 3.0 s before the last sample.
8. THE Audio_Renderer SHALL keep the absolute difference between any two consecutive samples of the same channel below 0.3 of full scale.
9. THE Audio_Renderer SHALL pass only finite sample values (no NaN or infinite values) to WAV encoding.
10. WHILE no other import or generation is running, WHEN the Backend receives a generation request for a Replay with a Target_Duration of 30 s, 2 min, or 3 min for the imported and selected Sample_Dataset SleepSession with the Default_Mapping and the default Random_Seed on the Reference_Machine, THE Backend SHALL finish storing the WAV file and Replay_Manifest in the Data_Directory within 60 s of receiving the request.
11. WHILE no other import or generation is running, WHEN the Backend receives a generation request for a Replay with a Target_Duration of 5 min or 10 min for the imported and selected Sample_Dataset SleepSession with the Default_Mapping and the default Random_Seed on the Reference_Machine, THE Backend SHALL finish storing the WAV file and Replay_Manifest in the Data_Directory within 180 s of receiving the request.
12. WHEN Replay generation succeeds, THE Backend SHALL store the WAV file and Replay_Manifest of the Replay in the Data_Directory before reporting the generation as complete.
13. IF the rendered audio contains a non-finite sample value, THEN THE Backend SHALL stop the generation without writing a WAV file or Replay_Manifest, leave previously stored Replays unchanged, and return a User_Error indicating a rendering failure.
14. IF writing the WAV file or Replay_Manifest to the Data_Directory fails, THEN THE Backend SHALL delete any partially written WAV file and Replay_Manifest of that Replay, leave previously stored Replays unchanged, and return a User_Error indicating the write failure and the Data_Directory location.

### Requirement 9: Determinism

**User Story:** As a user and contributor, I want the same night and settings to always produce the same replay, so that results are reproducible and comparable.

#### Acceptance Criteria

1. WHEN a Replay is generated twice from the same Generation_Inputs on the same machine with the same Sleep_Replay installation (same Sleep_Replay version and same pinned dependency versions), THE Backend SHALL produce a byte-identical WAV file and a byte-identical Replay_Manifest JSON document, regardless of whether each generation is requested through the Frontend or the CLI.
2. THE Sonification_Engine and the Audio_Renderer SHALL derive every procedural random choice from pseudo-random generators initialized from the Random_Seed at the start of each Replay generation and SHALL use no other source of randomness.
3. WHEN a Replay generation starts, THE Backend SHALL use the CLI Random_Seed option value if one is given, otherwise the random_seed value of the Mapping_Config if one is given, otherwise the default Random_Seed, and SHALL record the used value as the Random_Seed in the Replay_Manifest.
4. WHEN two Replays are generated from Generation_Inputs that differ only in the Random_Seed, THE Backend SHALL produce Replay_Manifests that are identical in every field except the Random_Seed and the random_seed value within the recorded Mapping_Config, including identical Coarse_State segments, Night_Events, environmental values per Feature_Window, per-metric availability, and warnings.
5. WHEN two Replays are generated from Generation_Inputs that differ only in the Random_Seed, THE Audio_Renderer SHALL produce WAV files with identical sample rate, bit depth, channel count, and sample count whose sample data differ in at least one sample value.
6. FOR ALL permutations of the order in which input files are provided, including the order of members within a zip archive and importing the SensorPush_CSV before or after the Fitbit_Export, THE Backend SHALL produce a byte-identical WAV file and a byte-identical Replay_Manifest JSON document (confluence property).
7. THE Backend SHALL produce WAV file bytes and Replay_Manifest JSON document bytes that are independent of the wall-clock time of generation, the system locale (including decimal separator and date format settings), the host machine timezone when the Display_Timezone is set explicitly, the output path, the Data_Directory location, and the Display_Units setting.
8. WHEN a Replay is rendered in a Backend process that has already rendered one or more other Replays, THE Backend SHALL produce a WAV file and Replay_Manifest byte-identical to those rendered from the same Generation_Inputs in a newly started Backend process after importing the same input files into an empty Data_Directory.
9. IF a Mapping_Config, a Settings_Panel value, or a CLI option specifies a Random_Seed that is not an integer from 0 to 4,294,967,295 inclusive, THEN THE Backend SHALL reject the generation request with a User_Error stating the allowed range, generate no Replay, and leave existing Replays unchanged.

### Requirement 10: Replay Manifest

**User Story:** As a developer, I want each replay to carry its own metadata, so that the Frontend can synchronize the timeline and anyone can reproduce the replay.

#### Acceptance Criteria

1. THE Backend SHALL create for each Replay exactly one Replay_Manifest containing: the SleepSession start_time and end_time; the Display_Timezone as an IANA timezone name; the Target_Duration; the Compression_Ratio; the Timeline_Resolution; the Coarse_State segments; every retained Night_Event with its type, Night_Time, Replay_Time, magnitude, and label; the environmental values per Feature_Window (one entry per Feature_Window in Replay_Time order, holding the mean smoothed temperature, humidity, and pressure of the Feature_Window in Canonical_Units, with an explicit missing indicator in place of a value where the metric is marked missing for every Aligned_Timeline sample in the Feature_Window); the per-metric availability (for each of the six Metrics, a status of unavailable when every Aligned_Timeline sample of the metric is marked missing and, for hrv_rmssd, no Session_HRV exists, otherwise available, plus the fraction of Aligned_Timeline samples marked missing from 0.0 to 1.0); the effective Mapping_Config with default values applied for omitted keys; the Random_Seed used, including the default Random_Seed when none was specified; the Input_Fingerprint; the Sleep_Replay software version; and every warning from the Processing_Report and Replay generation as a plain-language description.
2. THE Manifest_Serializer SHALL serialize each Replay_Manifest to a UTF-8 JSON document and produce byte-identical documents for Replay_Manifests that are equal in every field.
3. WHEN the Manifest_Serializer receives a JSON document containing every field listed in criterion 1 with values satisfying criteria 7 and 8, THE Manifest_Serializer SHALL parse the document into a Replay_Manifest.
4. FOR ALL valid Replay_Manifests (satisfying criteria 1, 7, and 8), serializing then parsing SHALL produce a Replay_Manifest equal to the original in every field, with each timestamp representing the same instant and UTC offset and each numeric value exactly equal (round-trip property).
5. THE Backend SHALL exclude TelemetryPoints and Aligned_Timeline samples from the Replay_Manifest, limiting telemetry-derived content to the per-Feature_Window environmental values, per-metric availability, and Night_Events listed in criterion 1.
6. THE Backend SHALL record the generation time of each Replay as a timezone-aware timestamp in the Replay record of the Metadata_Store and exclude the generation time from the Replay_Manifest.
7. THE Backend SHALL express the SleepSession start_time and end_time and every Night_Time in the Replay_Manifest as ISO 8601 timestamps with seconds and the UTC offset of the Display_Timezone at that instant, and express every Replay_Time, the Target_Duration, and the Timeline_Resolution as a number of seconds.
8. THE Backend SHALL write the Coarse_State segments as entries of start Replay_Time, end Replay_Time, and Coarse_State, ordered by start Replay_Time, non-overlapping, and together covering 0 s to the Target_Duration without gaps.
9. IF a document passed to the Manifest_Serializer is not valid JSON, lacks a field listed in criterion 1, contains a Night_Event magnitude outside 0.0 to 1.0, contains a Replay_Time outside 0 s to the Target_Duration, or contains Coarse_State segments that violate criterion 8, THEN THE Manifest_Serializer SHALL reject the document with an error identifying at least one missing or invalid field and return no Replay_Manifest.

### Requirement 11: Graceful Degradation

**User Story:** As a user with incomplete data, I want a replay from whatever data I have, so that a missing file or metric does not block the experience.

#### Acceptance Criteria

1. WHEN Replay generation is requested for a selected SleepSession that has Stage_Data, at least one heart_rate TelemetryPoint, or at least one temperature, humidity, or pressure TelemetryPoint, THE Backend SHALL generate a Replay of the Target_Duration in the default audio format (WAV, 44,100 Hz, 16-bit PCM, 2 channels) with peak ≤ −1 dBFS and overall RMS between −28 dBFS and −16 dBFS, regardless of how many Mapping_Config metric keys are Unavailable_Metrics.
2. IF Replay generation is requested for a selected SleepSession that has no Stage_Data, no heart_rate TelemetryPoint, and no temperature, humidity, or pressure TelemetryPoint (steps or HRV data alone being insufficient), THEN THE Backend SHALL return a User_Error stating that the session contains no usable data and naming Fitbit_Export sleep or heart rate files or a SensorPush_CSV as the data to import, and SHALL create no WAV file and no Replay record in the Metadata_Store.
3. IF a Mapping_Config metric key is an Unavailable_Metric, THEN THE Sonification_Engine SHALL hold that metric's contribution at the Neutral_Value from Replay_Time 0 to the Target_Duration.
4. WHEN a Replay is generated, THE Backend SHALL list every Unavailable_Metric by its Mapping_Config metric key in the Replay_Manifest, with an empty list when no metric is unavailable.
5. IF the selected SleepSession has no Stage_Data, THEN THE Sonification_Engine SHALL apply the Neutral preset from Replay_Time 0 to the Target_Duration while each metric that is not an Unavailable_Metric drives its mapped Sound_Parameter according to the Mapping_Config.
6. WHILE Fitbit_Export data and no SensorPush_CSV data has been imported, THE Sleep_Replay SHALL generate a Replay for the selected SleepSession from the Main_Screen Generate Replay button and from the CLI when only Fitbit_Export paths are passed, and SHALL add a warning to the Replay_Manifest stating that SensorPush environmental data is absent.
7. WHILE SensorPush_CSV data and no Fitbit_Export data has been imported, THE Sleep_Replay SHALL generate a Replay from the Main_Screen Generate Replay button for a SleepSession created from a manual time range, and SHALL add a warning to the Replay_Manifest stating that Fitbit data is absent.
8. IF the selected SleepSession has no Stage_Data, THEN THE Backend SHALL add a warning to the Replay_Manifest stating that stage data is unavailable and that the Neutral preset is used for the entire Replay.

### Requirement 12: Command-Line Interface

**User Story:** As a developer, I want to generate sample data and replays from the command line, so that I can script, test, and produce example audio without the UI.

#### Acceptance Criteria

1. WHEN the user runs the sample data command with a target directory path and an optional integer seed, THE CLI SHALL write the Sample_Dataset files produced by the Sample_Data_Generator into the directory, creating the directory if it does not exist, replacing existing files with the same names, leaving other files in the directory unchanged, and producing files byte-identical to the Sample_Dataset in the Repository when the seed is omitted.
2. THE CLI SHALL provide a generate command that accepts local paths to Fitbit_Export files (multiple JSON and CSV files or one zip archive) and to SensorPush_CSV files, requires at least one input path, applies no Max_Upload_Size limit, and accepts these optional options: a Mapping_Config file (YAML or JSON); a Target_Duration in seconds (30, 120, 180, 300, or 600); a Random_Seed (integer from 0 to 4,294,967,295); a Source_Timezone override per file type (IANA timezone name); a session date (YYYY-MM-DD); manual start and end times (ISO 8601 date-times, interpreted in the Display_Timezone when no offset is given), which cannot be combined with a session date; and an output path for the WAV file, next to which the CLI writes the Replay_Manifest JSON document with the same file name stem, replacing existing files.
3. WHEN the generate command runs without manual start and end times, THE CLI SHALL select the SleepSession chosen by the Session_Detector default rules (sleep-replay-data-pipeline Requirement 8, criteria 2 and 3), and when a session date is given, THE CLI SHALL instead select the most recent candidate SleepSession flagged as main sleep whose end_time falls on that date in the Display_Timezone, or the longest such candidate when none is flagged as main sleep.
4. WHEN a CLI command completes successfully, THE CLI SHALL exit with exit code 0 and print to standard output the absolute path of each written file, and for the generate command SHALL also print the selected SleepSession start and end times in ISO 8601 with the Display_Timezone offset, the Compression_Ratio rounded to one decimal place, the Night_Event count, and each Replay warning on its own line.
5. IF a command-line argument is missing or outside the values accepted in criteria 1 and 2, or file writing, import, session selection, processing, or rendering fails, THEN THE CLI SHALL print to standard error the User_Error code, description, affected file name where applicable, required action, and reference identifier for internal errors, leave existing files at the output path unchanged, write no new WAV file or Replay_Manifest to the output path, and exit with a non-zero exit code.
6. THE CLI SHALL take each generate setting from the command-line option when given, otherwise from the Mapping_Config file, and otherwise from the Default_Mapping and default parameter values (Target_Duration of 3 minutes, default Random_Seed, Data_Directory as output location), and SHALL ignore the settings persisted in the Metadata_Store.
7. WHEN the generate command receives manual start and end times, THE CLI SHALL use the SleepSession that the Session_Detector creates and validates for that time range as defined in sleep-replay-data-pipeline Requirement 8, criteria 8 and 9.
8. IF no candidate SleepSession has an end_time on the given session date in the Display_Timezone, THEN THE CLI SHALL report a User_Error as defined in criterion 5 whose required action lists the end dates of the available candidate SleepSessions.

### Requirement 13: Example Replay

**User Story:** As a new user or contributor, I want an example replay generated from the synthetic night included, so that I can hear the result immediately without personal data.

#### Acceptance Criteria

1. THE Repository SHALL include at least one example Replay WAV file generated from the Sample_Dataset with the Default_Mapping, together with a documented CLI command, stating the Target_Duration, Random_Seed, and Display_Timezone setting, that regenerates a byte-identical WAV file.

### Requirement 14: Automated Testing for Sonification and Audio

**User Story:** As a contributor, I want a comprehensive automated test suite, so that changes to processing or sound generation do not silently break the replay.

#### Acceptance Criteria

1. THE Test_Suite SHALL include at least one automated test for each of the following areas: Mapping_Config parsing and validation, sonification mappings, graceful degradation, deterministic audio generation, and the CLI.
2. THE Test_Suite SHALL include a property-based test for every acceptance criterion in this spec labeled as an invariant or as a round-trip, monotonicity, order-preservation, or confluence property, each running at least 100 generated cases (at least 5 where a single case renders a Replay or generates a full night with the Sample_Data_Generator) and reporting the generated input that caused any failure.
3. THE Test_Suite SHALL include an end-to-end test that imports the Sample_Dataset and generates a Replay for the default-selected SleepSession with the Default_Mapping, the default Random_Seed, and a 3-minute Target_Duration, and verifies that the WAV file matches the audio format listed in the Default Parameter Values, that its duration is within 50 ms of 180 s, that its peak and overall RMS levels are within the loudness limits listed in the Default Parameter Values, that the Manifest_Serializer parses the Replay_Manifest, and that the Replay_Manifest contains between 1 and Max_Event_Count Night_Events.
4. THE Test_Suite SHALL include a test that generates a Replay from the Sample_Dataset twice with the same Mapping_Config, the same Random_Seed, and a 30 s Target_Duration and verifies byte-identical WAV files and identical Replay_Manifests, then generates a third Replay with a different Random_Seed and verifies that its audio samples differ while its Coarse_State segments and Night_Events are identical.
5. THE Test_Suite SHALL run with one command documented in the README, use as input data only the Sample_Dataset, Sample_Data_Generator output, and synthetic fixture files stored in the Repository, and exit with a non-zero exit code when any test fails and a zero exit code when all tests pass.
6. WHILE outbound network access is disabled on the host, THE Test_Suite SHALL produce the same pass/fail result for every test as it does with network access enabled.
7. THE Test_Suite SHALL write every file and Metadata_Store database it creates to temporary directories, and leave the user's configured Data_Directory and the committed Repository files (including the Sample_Dataset and the example Replay) unmodified.
