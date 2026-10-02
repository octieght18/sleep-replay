import { api, jsonRequest, showError } from "./api.js";
export const METRICS = [
  "heart_rate",
  "hrv",
  "movement",
  "temperature",
  "humidity",
  "pressure",
];
export const TARGETS = [
  "pulse_rate",
  "rhythmic_density",
  "intensity",
  "transient_density",
  "brightness",
  "texture_density",
  "modulation",
  "none",
];
export const DEFAULT_TARGETS = {
  heart_rate: "pulse_rate",
  hrv: "modulation",
  movement: "transient_density",
  temperature: "brightness",
  humidity: "texture_density",
  pressure: "modulation",
};
export const DEFAULT_SENSITIVITIES = {
  heart_rate: 0.5,
  hrv: 0.3,
  movement: 0.8,
  temperature: 0.3,
  humidity: 0.3,
  pressure: 0.2,
};
export function validateSetting(kind, value) {
  if (value === "") return "Enter a value.";
  if (kind === "sound_style" && !["music", "nature"].includes(value))
    return "Choose ambient music or nature.";
  const number = Number(value);
  if (
    kind === "random_seed" &&
    (!Number.isInteger(number) || number < 0 || number > 4294967295)
  )
    return "Use an integer from 0 to 4294967295.";
  if (
    kind === "sensitivity" &&
    (!Number.isFinite(number) ||
      number < 0 ||
      number > 1 ||
      Math.abs(number * 20 - Math.round(number * 20)) > 1e-8)
  )
    return "Use 0.0 to 1.0 in steps of 0.05.";
  return null;
}
export class SettingsPanel {
  constructor(state, onChange) {
    this.state = state;
    this.onChange = onChange;
    this.queue = Promise.resolve();
    this.controls();
    document
      .getElementById("settings-form")
      .addEventListener("submit", (e) => e.preventDefault());
    document
      .getElementById("restore-defaults")
      .addEventListener("click", () => {
        const next = {
          ...this.state.settings,
          targets: { ...DEFAULT_TARGETS },
          sensitivities: { ...DEFAULT_SENSITIVITIES },
        };
        this.display(next);
        this.save(next);
      });
  }
  controls() {
    const container = document.getElementById("mapping-controls");
    for (const key of METRICS) {
      const row = document.createElement("div");
      row.className = "mapping-row";
      const label = document.createElement("label");
      label.textContent = key === "hrv" ? "HRV" : key.replaceAll("_", " ");
      label.htmlFor = `target-${key}`;
      const select = document.createElement("select");
      select.id = `target-${key}`;
      select.setAttribute("aria-label", key + " sound target");
      for (const target of TARGETS) {
        const option = document.createElement("option");
        option.value = target;
        option.textContent = target.replaceAll("_", " ");
        select.append(option);
      }
      const sensitivityLabel = document.createElement("label");
      const input = document.createElement("input");
      input.id = `sensitivity-${key}`;
      input.type = "number";
      input.min = "0";
      input.max = "1";
      input.step = "0.05";
      input.setAttribute("aria-label", key + " sensitivity");
      const error = document.createElement("small");
      error.id = `sensitivity-error-${key}`;
      error.className = "field-error";
      input.setAttribute("aria-describedby", error.id);
      sensitivityLabel.append(input, error);
      row.append(label, select, sensitivityLabel);
      container.append(row);
      select.addEventListener("change", () =>
        this.change("target", key, select, null),
      );
      input.addEventListener("input", () =>
        this.change("sensitivity", key, input, error),
      );
    }
    for (const [id, key, errorId] of [
      ["sound-style", "sound_style", "style-error"],
      ["target-duration", "target_duration", "duration-error"],
      ["random-seed", "random_seed", "seed-error"],
      ["display-units", "display_units", "units-error"],
    ]) {
      const input = document.getElementById(id);
      input.setAttribute(
        "aria-describedby",
        id === "sound-style" ? "style-help " + errorId : errorId,
      );
      input.addEventListener(id === "random-seed" ? "input" : "change", () =>
        this.change(key, null, input, document.getElementById(errorId)),
      );
    }
  }
  display(settings) {
    document.getElementById("sound-style").value =
      settings.sound_style || "music";
    document.getElementById("target-duration").value = settings.target_duration;
    document.getElementById("random-seed").value = settings.random_seed;
    document.getElementById("display-units").value = settings.display_units;
    for (const key of METRICS) {
      document.getElementById(`target-${key}`).value = settings.targets[key];
      document.getElementById(`sensitivity-${key}`).value =
        settings.sensitivities[key];
    }
    for (const error of document.querySelectorAll(".field-error"))
      error.textContent = "";
  }
  change(kind, key, input, error) {
    let message = validateSetting(kind, input.value);
    if (input.value === "" && kind === "random_seed")
      message = "Use an integer from 0 to 4294967295.";
    if (input.value === "" && kind === "sensitivity")
      message = "Use 0.0 to 1.0 in steps of 0.05.";
    if (error) error.textContent = message || "";
    input.setAttribute("aria-invalid", message ? "true" : "false");
    if (message) return;
    const previous = this.state.settings;
    const next = {
      ...previous,
      targets: { ...previous.targets },
      sensitivities: { ...previous.sensitivities },
    };
    if (kind === "target") next.targets[key] = input.value;
    else if (kind === "sensitivity")
      next.sensitivities[key] = Number(input.value);
    else
      next[kind] = ["display_units", "sound_style"].includes(kind)
        ? input.value
        : Number(input.value);
    this.save(next);
  }
  save(next) {
    this.state.settings = next;
    this.onChange(next);
    document.getElementById("settings-status").textContent = "Saving…";
    const job = this.queue
      .catch(() => {})
      .then(() => api("/api/settings", jsonRequest("PUT", next)));
    this.queue = job;
    this.state.pendingSettings = job;
    job
      .then(() => {
        document.getElementById("settings-status").textContent =
          "Saved locally.";
        document.getElementById("settings-error").hidden = true;
      })
      .catch((error) => {
        showError(document.getElementById("settings-error"), error);
        document.getElementById("settings-status").textContent =
          "Changes could not be saved. Retry the change before generating.";
      });
  }
}
