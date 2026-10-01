# Supported local export formats

Fitbit input is individual files or one ZIP. Names match case-insensitively on the final path component, at any ZIP directory depth. Unsupported files are listed and ignored. Encrypted, unreadable, oversized or malformed matching members are reported and skipped; archives are never extracted to arbitrary paths.

| Filename | Fields read | Default source timezone |
|---|---|---|
| `sleep-YYYY-MM-DD.json` | Array of logs: `logId`, `dateOfSleep`, `startTime`, `endTime`, `duration`, `type`, `isMainSleep`, `levels.data` and `levels.shortData` with `dateTime`, `level`, `seconds` | Display timezone |
| `heart_rate-YYYY-MM-DD.json` | Array: `dateTime` in `MM/DD/YY HH:MM:SS`, `value.bpm` | UTC |
| `steps-YYYY-MM-DD.json` | Array: `dateTime` in `MM/DD/YY HH:MM:SS`, numeric/string `value` | UTC |
| `Heart Rate Variability Details - YYYY-MM-DD.csv` | `timestamp` ISO date/time and `rmssd` milliseconds; other columns ignored | Display timezone |
| `Daily Heart Rate Variability Summary - YYYY-MM(-DD).csv` | `timestamp` date and `rmssd`; nightly summary attached on the session end date when intraday HRV is absent | Display timezone |

Sleep dates/times use ISO 8601. Two-digit intraday years mean 2000–2099. Explicit timestamp offsets take precedence over overrides. Fitbit JSON must have an array at the top level. Required row fields missing or unparseable timestamps cause row skips; absent metrics cause warnings without preventing import.

Stage mapping: `wake` and `awake` → awake; `light` and `core` → light; `deep` → deep; `rem` → rem; classic `asleep` → asleep; `restless` → restless; other names → unknown with a warning. Short-data entries override the underlying stage as awake; segments are clipped to session bounds and gaps remain unknown. Classic asleep and restless use the Light sound preset. Logs without usable stages use Unknown and the Neutral sound preset.

## SensorPush CSV

Comma or semicolon delimiter, optional UTF-8 BOM, one header row. Header matching is case-insensitive; the leftmost matching column of each type is used, with a warning about duplicates.

| Column | Header keywords | Recognized units |
|---|---|---|
| Timestamp, required | `timestamp`, `observed`, `datetime`, `date`, `time` | — |
| Temperature | contains `temp`, excludes `dew` | `°F`, standalone `F`, `Fahrenheit`, `°C`, standalone `C`, `Celsius` |
| Relative humidity | `humidity` or standalone token `rh`, excludes `absolute` | `%` |
| Barometric pressure | `pressure` or `baro` | `inHg`, `mbar`, `hPa`, `kPa` |
| Sensor identifier, optional | `sensor id`, `sensorid`, `sensor`, `device` | — |

Dew point, VPD/vapor/vapour/deficit, absolute humidity and unknown columns are ignored. Measurement and sensor keywords take precedence over broad date/time matching. A missing sensor identifier uses the filename as the source identity.

Accepted timestamps: `YYYY-MM-DD HH:MM[:SS]`, `YYYY-MM-DDTHH:MM[:SS][±HH:MM|Z]`, `M/D/YYYY h:mm[:ss] AM/PM`, `M/D/YYYY HH:MM[:SS]`. Day-first dates and epoch numeric timestamps are unsupported. Offset-free readings use the display timezone unless the `sensorpush` source override is supplied.

Headers without units trigger inference and a warning: temperature median above 45 means Fahrenheit, otherwise Celsius; pressure median 25–32 means inHg, 90–110 means kPa, 900–1100 means hPa. Other pressure medians make pressure unavailable rather than guessing. Humidity is percent. Add explicit units when an inference is wrong. Internal units are Celsius, percent and hPa; Fahrenheit converts with `(F−32)×5/9`, inHg multiplies by `33.8638866667`, kPa multiplies by 10 and mbar equals hPa.

Validation ranges: heart rate 20–250 bpm, HRV greater than 0 and at most 500 ms, steps 0–300 per minute. SensorPush accepts finite numeric cells without an additional plausibility-range gate. Invalid values are excluded and counted. A row with no valid measurement is skipped; a CSV producing no measurements raises `NO_READABLE_ROWS`.

## Limits and selection

The browser enforces 2 GiB per file. A matching ZIP member may contain at most 200 MiB uncompressed. Only supported members are read. Export unrelated activities, profile data, dew point, VPD and live cloud API data are outside this format contract.

Duplicate sessions are removed and overlapping sessions merged. Candidates are presented in detector order; automatic selection prefers the newest qualifying overnight session with main-sleep status and duration used as tie breakers. Processing deduplicates points, selects the sensor with most in-session measurements for each metric, interpolates short gaps and keeps longer gaps missing. Heart rate gaps above five minutes and HRV/environment gaps above fifteen minutes remain missing.
