import { api, showError } from "./api.js";
import { ImportView } from "./import_view.js";
import { MainScreen } from "./main_screen.js";
import { SettingsPanel } from "./settings_panel.js";
import { Timeline } from "./timeline.js";

const state = { settings: null, sessions: null, pendingSettings: null };
function showView(name) {
  for (const view of ["main", "import", "settings"])
    document.getElementById(view + "-view").hidden = view !== name;
  for (const button of document.querySelectorAll("[data-view]"))
    button.setAttribute("aria-selected", button.dataset.view === name);
}
for (const button of document.querySelectorAll("[data-view]"))
  button.addEventListener("click", () => showView(button.dataset.view));
const timeline = new Timeline();
const main = new MainScreen(state, timeline);
const imports = new ImportView(
  state,
  async (data) => {
    main.unload();
    main.setSessions(data);
  },
  async () => {
    main.unload();
    main.setSessions(null);
    showView("main");
  },
  () => showView("main"),
);
const settings = new SettingsPanel(state, (next) => {
  timeline.units = next.display_units;
  if (!timeline.manifest) timeline.duration = next.target_duration;
  timeline.draw();
});
async function start() {
  const controls = [
    ...document.querySelectorAll(
      "#settings-form input,#settings-form select,#restore-defaults,#import-form input,#import-button,#sample-button",
    ),
  ];
  controls.forEach((c) => (c.disabled = true));
  try {
    const [status, current] = await Promise.all([
      api("/api/status"),
      api("/api/settings"),
    ]);
    state.settings = current;
    settings.display(current);
    timeline.units = current.display_units;
    timeline.duration = current.target_duration;
    timeline.draw();
    imports.configure(status);
    if (status.has_imports) {
      try {
        main.setSessions(await api("/api/sessions"));
      } catch (error) {
        if (error.code !== "NO_SLEEP_SESSION") throw error;
        main.setSessions(null);
      }
      showView("main");
    } else showView("import");
    controls.forEach((c) => (c.disabled = false));
  } catch (error) {
    showError(document.getElementById("global-error"), error);
  }
}
await start();
