# Mapping configuration

Use [mapping-config.example.yaml](../mapping-config.example.yaml) with `--config`. UTF-8 YAML and JSON are accepted; omitted keys use defaults and unknown keys are rejected. The browser edits targets and sensitivities while retaining the default smoothing/hysteresis values. Invalid documents are rejected as a whole.

| Metric key | Default target | Sensitivity | Smoothing, night minutes | Hysteresis, canonical units |
|---|---|---:|---:|---:|
| `heart_rate` | pulse_rate | 0.5 | 5 | 1.0 bpm |
| `hrv` | modulation | 0.3 | 15 | 2.0 ms |
| `movement` | transient_density | 0.8 | — | — |
| `temperature` | brightness | 0.3 | 15 | 0.1 °C |
| `humidity` | texture_density | 0.3 | 15 | 0.5 percentage points |
| `pressure` | modulation | 0.2 | 15 | 0.1 hPa |

All six metrics can target any of `pulse_rate`, `rhythmic_density`, `intensity`, `transient_density`, `brightness`, `texture_density`, `modulation`, `none`. Sensitivity is a finite number 0–1; browser controls additionally require 0.05 steps. Smoothing accepts integer 1–60 minutes. Hysteresis maxima are respectively 10 bpm, 20 ms, 1 °C, 5 percentage points and 1 hPa for the five continuous metrics; minimum is zero. Movement accepts only target and sensitivity. Defaults: `target_duration: 180` (allowed 30, 120, 180, 300, 600 seconds), `random_seed: 20240301` (unsigned 32-bit integer).

## Direction, neutral and combination

Every metric's contribution is nondecreasing with its normalized value before hysteresis and rate limits. For normalized value n and sensitivity s it is `clamp(0.5 + s × (n − 0.5), 0, 1)`; pressure uses half that excursion. Normalization is relative to the night's smoothed range, widened to minimum spans of 10 bpm, 10 ms, 2 °C, 10 percentage points and 3 hPa. Movement already lies in 0–1. Session HRV provides a constant `rmssd/100` contribution when intraday HRV is absent.

Neutral is **0.5** for every parameter. `none`, zero sensitivity and wholly unavailable data contribute neutral. Missing windows glide toward neutral over one replay second and glide back on recovery. Hysteresis holds the last accepted contribution until raw smoothed change reaches its threshold. Multiple metrics assigned to one parameter combine as their sensitivity-weighted arithmetic mean; no contributors means neutral. Restless windows add `0.25 × movement sensitivity` to texture/transient density when movement is enabled, clamped to one. This shared boost remains independent of the movement target. Final trajectories change at most 0.5/second for the four fast parameters and 0.2/second for the three slow parameters.

| Sound parameter | Layer and audible property |
|---|---|
| pulse_rate | Sine pulse speed: 0.5–2 Hz (neutral 1.25 Hz) |
| rhythmic_density | Additional half-cycle pulse activity |
| intensity | Pad/texture layer gain; global loudness normalization remains applied |
| transient_density | Density of soft movement blips |
| brightness | Pad upper harmonics |
| texture_density | Filtered noise gain/grain activity |
| modulation | Pad/texture slow amplitude modulation depth |

The four voices are additive pad, sine pulse, filtered noise texture and soft blips. State changes select sound character with up to two-second crossfades; events add bounded motifs. Awake → Awake, Light/Asleep/Restless → Light, Deep → Deep, REM → REM, Unknown → Neutral. Presets have these final backend values:

| Preset | Bass gain | Upper gain | Harmonic gain | Texture gain | Cutoff Hz | Modulation Hz | Depth | Note interval s |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Deep | 1.3 | 0.02 | 0 | 0.012 | 120 | 0.025 | 0.08 | 5 |
| Light | 0.35 | 0.8 | 0.18 | 0.04 | 900 | 0.07 | 0.12 | 3 |
| Neutral | 0.82 | 0.4 | 0.08 | 0.025 | 400 | 0.045 | 0.10 | 4 |
| REM | 0.6 | 0.5 | 0.12 | 0.045 | 650 | 0.11 | 0.55 | 3 |
| Awake | 0.5 | 0.55 | 0.14 | 0.025 | 700 | 0.06 | 0.10 | 10 |

Other final defaults: timeline resolution candidates 1/5/10/15/30/60 seconds; interpolation gap 5 minutes for heart rate, 15 minutes for HRV/environment; feature windows 0.5 replay seconds; minimum coarse-state duration 1 replay second; wake event 5 night minutes; major transition 10 night minutes; movement transient threshold 0.1, burst threshold 0.6, restless threshold 0.3 sustained 10 night minutes. Environmental event thresholds: 1 °C within 30 minutes, 5 humidity points within 30 minutes, 1 hPa within three hours. Same-type events merge within 15 night minutes; maximum twelve events. Missing-data glide 1 replay second; neutral 0.5. WAV is stereo 44,100 Hz signed 16-bit PCM, target RMS −22 dBFS, allowed RMS −28 to −16 dBFS, peak limit 0.891.

These sounds represent measurements artistically; they provide no medical interpretation or health advice.
