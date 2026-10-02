# Import a night and listen

1. Start the app using the [README](../README.md), then open http://127.0.0.1:8734.
2. On first start choose **Use sample data**. Read the import report and choose **Open replay**.
3. Choose a session if more than one appears. Open **Settings** to choose **Ambient music** or **Nature**, a duration, sound mappings, seed, or display units. Changes save locally without generating audio.
4. Return to **Replay**, choose **Generate replay**, then **Play**. Generation loads at 00:00 and does not autoplay.
5. Click the timeline or an event marker to seek. Focus the timeline and use Left/Right for five seconds, Home for the beginning, and End for the end. Markers show their labels on hover or keyboard focus.

## Fitbit through Google Takeout

1. Sign into your Google account at https://takeout.google.com. Deselect other products and select Fitbit. Choose the export data types containing sleep, heart rate, steps and heart rate variability.
2. Choose a one-time ZIP export and download it when Google makes it available. Export screens and product names can change; a Fitbit account export is also supported if its files match [data-formats.md](data-formats.md).
3. In **Import**, select one ZIP under Fitbit, or extract it and select the supported JSON/CSV files together. A Fitbit ZIP cannot be combined with other Fitbit files. SensorPush CSV files may accompany the ZIP.
4. Expand **Source timezones**. Sleep and HRV default to the display timezone; intraday heart rate and steps default to UTC. Change an override to the IANA zone actually used by offset-free timestamps, such as `America/New_York`.
5. Choose **Import files**. Review accepted files, unsupported/skipped files and their reasons, point counts, coverage and warnings. The newest qualifying night is selected. Choose **Open replay** and generate it.

For CLI import use `python -m backend.api.cli generate fitbit.zip sensorpush.csv --output night.wav`; see [cli.md](cli.md) for timezone and session options.

## SensorPush CSV export

1. Open the SensorPush app or web dashboard and select your bedroom sensor and the date range covering the night.
2. Export readings as CSV. Include a date/time column and any available temperature, relative humidity and barometric pressure columns. Prefer explicit units in the headers.
3. Select one or more exports under SensorPush in **Import**. Confirm the SensorPush source timezone for timestamps without offsets.
4. Import alongside Fitbit sleep data. If there are no sleep logs, the telemetry is retained and the app offers a manual start/end range in the display timezone. A range must have an end after its start and last at most 24 hours.
5. Generate and play the replay. Missing measurements are listed; the app does not infer health meaning from them.

The browser limit is **2 GiB per file**. For a larger Takeout archive extract just the supported files, or use the CLI. Matching ZIP members are individually limited to 200 MiB uncompressed; the CLI does not bypass that safety limit. Imports remain local. Failed uploads keep the selected browser files and timezone fields for retry.

## Timezones and settings

Set `SLEEP_REPLAY_DISPLAY_TIMEZONE` before starting the app to choose the zone used for session dates and labels. Local startup otherwise uses the host zone; Docker defaults to UTC. Source overrides affect interpretation of offset-free input timestamps; explicit offsets take precedence. The sample action applies its own `America/New_York` overrides even on a host in another zone.

Sensitivity ranges from 0 to 1 in steps of 0.05. A target of `none` disables that metric's contribution. **Restore mapping defaults** resets targets and sensitivities only; sound style, duration, seed and display units stay as chosen. Units change labels immediately without generating another replay. Settings and imported sessions survive restart. Play at the end restarts from the beginning.

Nature provides continuous wind and waves. Variation from the night's usual temperature, humidity and pressure adds rustling leaves, soft rain and distant rumble, with gradual transitions through missing data and stage changes. Its original sounds are CC0, permitting research reuse. See [nature sound and licensing](nature-sound.md) for the mappings, continuity, and license scope.

Sleep Replay is not a medical device and provides no health advice, diagnosis or treatment recommendations.
