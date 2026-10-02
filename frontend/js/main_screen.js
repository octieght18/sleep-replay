import { api, jsonRequest, showError } from "./api.js";
import { formatNight } from "./timeline.js";
export function sessionLabel(session, zone, now = new Date()) {
  const date = new Intl.DateTimeFormat("en-CA", {
    timeZone: zone,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  });
  if (date.format(new Date(session.end_time)) === date.format(now))
    return "Last night";
  return (
    "Night of " +
    new Intl.DateTimeFormat("en-US", {
      timeZone: zone,
      month: "short",
      day: "numeric",
      year: "numeric",
    }).format(new Date(session.start_time))
  );
}
export class MainScreen {
  constructor(state, timeline) {
    this.state = state;
    this.timeline = timeline;
    document
      .getElementById("generate-button")
      .addEventListener("click", () => this.generate());
    document
      .getElementById("session-selector")
      .addEventListener("change", (e) => this.select(e.target.value));
    document.getElementById("manual-form").addEventListener("submit", (e) => {
      e.preventDefault();
      this.manual();
    });
  }
  setSessions(data) {
    this.state.sessions = data;
    const session = data?.selected;
    document.getElementById("manual-panel").hidden = !!session;
    document.getElementById("generate-button").disabled = !session;
    document.getElementById("replay-title").textContent = session
      ? sessionLabel(session, data.display_timezone)
      : "Your night, replayed.";
    document.getElementById("session-range").textContent = session
      ? `${formatNight(new Date(session.start_time), data.display_timezone)} → ${formatNight(new Date(session.end_time), data.display_timezone)}`
      : "Import sleep logs or define a manual range.";
    const select = document.getElementById("session-selector");
    select.replaceChildren();
    for (const row of data?.sessions || []) {
      const option = document.createElement("option");
      option.value = row.key;
      option.textContent = `${sessionLabel({ ...row, end_time: row.start_time }, data.display_timezone, new Date(0))} · ${formatNight(new Date(row.start_time), data.display_timezone)} → ${formatNight(new Date(row.end_time), data.display_timezone)} · ${row.duration_text}`;
      select.append(option);
    }
    if (session) select.value = data.selected_id;
    document.getElementById("session-selector-wrap").hidden =
      (data?.sessions?.length || 0) < 2;
  }
  unload() {
    this.timeline.unload(this.state.settings.target_duration);
    document.getElementById("replay-style").hidden = true;
    document.getElementById("replay-warnings").hidden = true;
    document.getElementById("main-error").hidden = true;
  }
  async select(id) {
    const previous = this.state.sessions.selected_id;
    try {
      const data = await api(
        "/api/sessions/selected",
        jsonRequest("PUT", { id }),
      );
      this.unload();
      this.setSessions(data);
    } catch (error) {
      document.getElementById("session-selector").value = previous;
      showError(document.getElementById("main-error"), error);
    }
  }
  async manual() {
    try {
      const data = await api(
        "/api/sessions/manual",
        jsonRequest("POST", {
          start: document.getElementById("manual-start").value,
          end: document.getElementById("manual-end").value,
        }),
      );
      this.unload();
      this.setSessions(data);
    } catch (error) {
      showError(document.getElementById("main-error"), error);
    }
  }
  async generate() {
    this.timeline.audio.pause();
    this.timeline.setBusy(true);
    document.getElementById("main-error").hidden = true;
    const controls = [
      document.getElementById("generate-button"),
      document.getElementById("session-selector"),
    ];
    controls.forEach((c) => (c.disabled = true));
    document.getElementById("generation-progress").hidden = false;
    try {
      if (this.state.pendingSettings) await this.state.pendingSettings;
      const replay = await api("/api/replays", { method: "POST" });
      const manifest = await api(replay.manifest_url);
      this.timeline.load(manifest, replay.audio_url);
      const style = document.getElementById("replay-style");
      style.textContent =
        manifest.mapping_config.sound_style === "nature"
          ? "Nature · CC0 audio"
          : "Ambient music";
      style.hidden = false;
      const warnings = document.getElementById("replay-warnings");
      warnings.replaceChildren();
      const lines = [
        ...manifest.warnings,
        ...(manifest.unavailable_metrics.length
          ? [`Unavailable metrics: ${manifest.unavailable_metrics.join(", ")}`]
          : []),
      ];
      for (const line of lines) {
        const p = document.createElement("p");
        p.textContent = line;
        warnings.append(p);
      }
      warnings.hidden = !lines.length;
    } catch (error) {
      showError(document.getElementById("main-error"), error);
    } finally {
      controls.forEach((c) => (c.disabled = false));
      document.getElementById("generation-progress").hidden = true;
      this.timeline.setBusy(false);
    }
  }
}
