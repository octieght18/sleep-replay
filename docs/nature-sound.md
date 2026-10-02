# Nature sound and research reuse

In **Settings → Sound style**, choose **Nature · waves, wind & rain**, then
generate a replay. Ambient music remains the default. The selection survives
restart; switching style generates a distinct replay, while returning to a
previously generated configuration reuses its cached WAV. Changing settings
does not replace the audio already loaded in the player.

Nature uses original synthesized surf, wind, soft rain, leaf rustle, and a low
distant rumble. It uses no downloaded recordings, sample packs, sound APIs,
or new dependencies. It runs offline and locally with the usual Python setup;
Docker stays optional.

## How the night shapes the sound

The existing metric mappings work in both styles:

| Shared parameter | Nature property |
|---|---|
| `pulse_rate` | Pace of surf swells, approximately 8–17 seconds per wave |
| `rhythmic_density` | Surf activity |
| `intensity` | Wind strength |
| `transient_density` | Leaf rustle; movement events add soft rustling bursts |
| `brightness` | Air detail above the low wind |
| `texture_density` | Rain texture |
| `modulation` | Gust depth |

Environmental departure adds a second, gradual influence. Each available
smoothed temperature, humidity, or pressure value is compared with that
metric's median for the selected night. Its variation score is
`min(1, abs(value − median) / scale) × sensitivity`. Scales are **1 °C**,
**5 relative-humidity percentage points**, and **1 hPa**, respectively.
Temperature variation adds leaves, humidity variation adds rain, and pressure
variation adds wind and low rumble. Either direction counts as a departure.
These scores represent variation within the night, rather than outdoor weather,
medical severity, or a separate radar measurement.

A target of `none` or sensitivity of zero disables both contributions from
that metric. The extra environmental scores use smoothed values; hysteresis
continues to apply to the shared mapping trajectories. Missing environmental
windows return toward zero variation; wholly unavailable metrics contribute
none. A steady wind/surf bed remains audible without environmental data.
Stage changes crossfade the scene's activity, and sleep/wake events add soft
noise swells. No musical note carriers are used in nature mode.

## Continuity and reproducibility

Noise streams run through the whole replay, with independent seed-derived
streams for each layer and stereo channel. They never restart at rendering
block or state boundaries and contain no repeated sample loops. Smooth noise
interpolation, sample-by-sample stage crossfades, gentle movement envelopes,
and environmental rate limits of **0.08 per replay second** prevent abrupt
changes. The normal two-second opening and three-second ending fades remain.
Nature WAVs follow the same 44.1 kHz, stereo, 16-bit PCM loudness and continuity
checks as music.

The same input, settings, seed, and renderer version generate identical audio.
Nature uses renderer version **0.3.0** and provenance generator
`sleep-replay-nature-v1`. Music keeps version **0.2.0** and its original output
and manifests. The package version is 0.3.0. Nature manifests contain
`mapping_config.sound_style: "nature"` and `sound_provenance`, which identifies
the procedural source, layers, generator, and **CC0-1.0** license. The stored
input fingerprint, effective settings and seed identify the rendering inputs.

## Sound license

The project's original procedural nature sound material and its generated
nature WAVs are dedicated under **CC0 1.0 Universal**, to the extent the project
holds rights. This permits copying, modification, redistribution, publication,
and research or commercial use, with no attribution requirement. The full
legal text and dedication scope are in [LICENSE-SOUNDS](../LICENSE-SOUNDS).
The application code retains its [MIT license](../LICENSE).

No third-party recording needs clearance or attribution. Imported datasets
retain their own rights; the sound dedication does not license those datasets.
For reproducible research, retain the WAV's JSON manifest and cite Sleep Replay,
the Git revision, generator version, mapping configuration and seed. Avoid
publishing personal measurements without the appropriate rights.

License sources: the [Creative Commons CC0 summary](https://creativecommons.org/publicdomain/zero/1.0/),
[CC0 legal code](https://creativecommons.org/publicdomain/zero/1.0/legalcode.en),
and the [SPDX CC0 legal text](https://github.com/spdx/license-list-data/blob/main/text/CC0-1.0.txt).

## CLI and sample

Add `--sound-style nature` to the usual generate command, or set
`sound_style: nature` in a YAML/JSON mapping file. An explicit CLI choice
overrides the file. `--sound-style music` selects the original renderer.

The [30-second nature example](../examples/nature_replay.wav) and its
[manifest](../examples/nature_replay.json) use the synthetic sample night:

```bash
python -m backend.api.cli generate "sample_data/sleep-2024-03-01.json" "sample_data/heart_rate-2024-03-01.json" "sample_data/steps-2024-03-01.json" "sample_data/Heart Rate Variability Details - 2024-03-01.csv" "sample_data/sensorpush.csv" --sound-style nature --target-duration 30 --seed 20240301 --display-timezone America/New_York --fitbit-heart-rate-timezone America/New_York --fitbit-steps-timezone America/New_York --output examples/nature_replay.wav
```
