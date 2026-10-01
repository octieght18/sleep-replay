# Local data, exposure and deletion

There is no authentication. Default servers bind `127.0.0.1`; the Docker published ports also bind `127.0.0.1`. `SLEEP_REPLAY_API_HOST` and `SLEEP_REPLAY_FRONTEND_HOST` can explicitly override binding, with a startup warning that imports and replays may become reachable by other devices. Use the default for personal data. An Origin header must match the configured frontend origin; rejection returns an actionable error and performs no operation. `SLEEP_REPLAY_FRONTEND_ORIGIN` changes that allowed origin when intentionally hosting elsewhere.

Runtime imports, processing, rendering and playback use no external services, tracking, fonts or hosted scripts. Network access is needed to clone/install dependencies or build Docker images. Browser requests stay on the frontend origin; its local proxy communicates with the backend. Logs record operational metadata only, without telemetry values or sample timestamps. Unexpected failures return a reference ID rather than exposing a stack trace or raw input.

## Data directory

| Platform | Default |
|---|---|
| Windows | `%LOCALAPPDATA%\SleepReplay` (fallback `%USERPROFILE%\AppData\Local\SleepReplay`) |
| macOS | `~/Library/Application Support/SleepReplay` |
| Linux | `$XDG_DATA_HOME/sleep-replay`, or `~/.local/share/sleep-replay` |
| Docker | `/data` in the Compose `sleep-replay-data` named volume |

Set `SLEEP_REPLAY_DATA_DIR` to an absolute directory before local startup to override it. Startup creates/checks the directory and fails before accepting requests when unwritable. Using `data/` or `sleep-replay-data/` inside the clone is also supported; both are ignored by Git and Docker. Keep any other custom location outside the clone or add it to your own ignore rules.

The directory holds `metadata.sqlite3` (imports, sessions, settings and replay records), normalized telemetry under `telemetry/`, WAV/JSON pairs under `replays/`, metadata-only logs under `logs/`, and transient upload files under `uploads/` that are removed after each request. SQLite may also create journal/WAL files. Original exports remain where you downloaded them. Explicit CLI `--output` exports remain at the chosen destination. Back up the whole data directory while the app is stopped if you need to retain it.

`docker compose down` preserves data. To delete **all app data**, first stop the app and confirm the directory being removed is the one you use. For default Windows storage:

```powershell
Remove-Item -LiteralPath "$env:LOCALAPPDATA\SleepReplay" -Recurse -Force
```

Default Linux storage:

```bash
rm -rf -- "${XDG_DATA_HOME:-$HOME/.local/share}/sleep-replay"
```

macOS:

```bash
rm -rf -- "$HOME/Library/Application Support/SleepReplay"
```

Docker (includes settings, imports and replays):

```bash
docker compose down --volumes
```

For an override remove the exact directory configured in `SLEEP_REPLAY_DATA_DIR` instead. Exported WAV/JSON copies and downloaded input files must be deleted separately if desired. There is no encryption-at-rest feature; use operating-system disk protection as needed.
