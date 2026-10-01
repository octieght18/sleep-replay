import { api, showError } from "./api.js";
export const FILE_TYPES = [
  "fitbit_sleep",
  "fitbit_heart_rate",
  "fitbit_steps",
  "fitbit_hrv_details",
  "fitbit_hrv_summary",
  "sensorpush",
];
export function validateFiles(fitbit, sensor, limit) {
  for (const [files, allowed] of [
    [fitbit, ["json", "csv", "zip"]],
    [sensor, ["csv"]],
  ])
    for (const file of files) {
      if (!allowed.includes(file.name.split(".").pop().toLowerCase()))
        return {
          description: "The selected file type is unsupported.",
          file_name: file.name,
          action:
            "Choose Fitbit JSON/CSV files or one ZIP, and SensorPush CSV files.",
        };
      if (file.size > limit)
        return {
          description: "The file exceeds the 2 GB upload limit.",
          file_name: file.name,
          action:
            "Import through python -m backend.api.cli generate, or select only supported Fitbit files.",
        };
    }
  if (
    fitbit.some((file) => file.name.toLowerCase().endsWith(".zip")) &&
    fitbit.length !== 1
  )
    return {
      description: "A Fitbit ZIP cannot be combined with other Fitbit files.",
      file_name: fitbit.map((f) => f.name).join(", "),
      action: "Choose one Fitbit ZIP or individual JSON/CSV files.",
    };
  if (!fitbit.length && !sensor.length)
    return {
      description: "No files are selected.",
      action: "Choose exports or use sample data.",
    };
  return null;
}
export class ImportView {
  constructor(state, onImported, onNoSession, onOpen) {
    this.state = state;
    this.onImported = onImported;
    this.onNoSession = onNoSession;
    this.onOpen = onOpen;
    document.getElementById("import-form").addEventListener("submit", (e) => {
      e.preventDefault();
      this.import(false);
    });
    document
      .getElementById("sample-button")
      .addEventListener("click", () => this.import(true));
  }
  configure(status) {
    this.status = status;
    document.getElementById("display-zone").textContent =
      `Display: ${status.display_timezone}`;
    const fields = document.getElementById("timezone-fields");
    fields.replaceChildren();
    for (const key of FILE_TYPES) {
      const label = document.createElement("label");
      label.textContent = key.replaceAll("_", " ");
      const input = document.createElement("input");
      input.dataset.fileType = key;
      input.value = status.source_timezones[key];
      input.setAttribute("aria-label", key + " timezone");
      label.append(input);
      fields.append(label);
    }
  }
  async import(sample) {
    const error = document.getElementById("import-error");
    error.hidden = true;
    const fitbit = [...document.getElementById("fitbit-files").files],
      sensor = [...document.getElementById("sensorpush-files").files];
    const validation = sample
      ? null
      : validateFiles(fitbit, sensor, this.status.max_upload_bytes);
    if (validation) {
      showError(error, validation);
      return;
    }
    let options = { method: "POST" };
    if (!sample) {
      const form = new FormData();
      for (const file of fitbit) form.append("fitbit", file);
      for (const file of sensor) form.append("sensorpush", file);
      const overrides = {};
      for (const input of document.querySelectorAll("[data-file-type]"))
        if (input.value.trim())
          overrides[input.dataset.fileType] = input.value.trim();
      form.append("timezone_overrides", JSON.stringify(overrides));
      options.body = form;
    }
    const controls = [
      ...document.querySelectorAll(
        "#import-form input,#import-form button,#sample-button",
      ),
    ];
    controls.forEach((c) => (c.disabled = true));
    document.getElementById("import-progress").hidden = false;
    try {
      const response = await api(
        sample ? "/api/imports/sample" : "/api/imports",
        options,
      );
      this.report(response.report);
      await this.onImported(response);
    } catch (failure) {
      showError(error, failure);
      if (failure.code === "NO_SLEEP_SESSION") await this.onNoSession();
    } finally {
      controls.forEach((c) => (c.disabled = false));
      document.getElementById("import-progress").hidden = true;
    }
  }
  report(report) {
    const element = document.getElementById("import-report");
    element.replaceChildren();
    const heading = document.createElement("h2");
    heading.textContent = "Import complete";
    element.append(heading);
    const open = document.createElement("button");
    open.type = "button";
    open.className = "secondary";
    open.textContent = "Open replay";
    open.addEventListener("click", this.onOpen);
    element.append(open);
    const lines = [
      ...report.accepted_files.map((name) => `Accepted: ${name}`),
      ...report.skipped_files.map((f) => `Skipped: ${f.name} — ${f.reason}`),
      ...report.unsupported_files.map((name) => `Unsupported: ${name}`),
      `Skipped: ${report.skipped_value_count} values, ${report.skipped_row_count} rows; ${report.unsupported_count} unsupported files`,
      ...Object.entries(report.per_metric_counts).map(([key, count]) => {
        const coverage = report.per_metric_coverage[key];
        return `${key}: ${count} points${coverage ? ` · ${coverage.start} → ${coverage.end}` : ""}`;
      }),
      ...report.warnings.map(
        (w) => `Warning: ${w.description} ${w.recommended_action}`,
      ),
    ];
    for (const line of lines) {
      const p = document.createElement("p");
      p.textContent = line;
      element.append(p);
    }
    element.hidden = false;
  }
}
