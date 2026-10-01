import { environmentalValues } from "./units.js";
import { showError } from "./api.js";
export const STATE_NAMES = {
  awake: "Awake",
  light: "Light Sleep",
  deep: "Deep Sleep",
  rem: "REM",
  asleep: "Asleep",
  restless: "Restless",
  unknown: "Unknown",
};
export const clamp = (value, duration) =>
  Math.min(duration, Math.max(0, value));
export const clock = (value) =>
  `${Math.floor(value / 60)
    .toString()
    .padStart(2, "0")}:${Math.floor(value % 60)
    .toString()
    .padStart(2, "0")}`;
export function segmentAt(segments, time, duration) {
  return segments.find(
    ([a, b]) =>
      a <= time && (time < b || (time === duration && b === duration)),
  );
}
export function nightTime(manifest, position) {
  return new Date(
    Date.parse(manifest.session_start) +
      position * manifest.compression_ratio * 1000,
  );
}
export function formatNight(date, zone) {
  return new Intl.DateTimeFormat("en-US", {
    timeZone: zone,
    hour: "numeric",
    minute: "2-digit",
    hour12: true,
  }).format(date);
}
export function seekKey(key, position, duration) {
  return clamp(
    {
      ArrowLeft: position - 5,
      ArrowRight: position + 5,
      Home: 0,
      End: duration,
    }[key] ?? position,
    duration,
  );
}
export function markerPosition(event, duration) {
  return (clamp(event.replay_time_s, duration) / duration) * 100;
}

export class Timeline {
  constructor(root = document) {
    this.root = root;
    this.audio = root.getElementById("replay-audio");
    this.element = root.getElementById("timeline");
    this.play = root.getElementById("play-button");
    this.manifest = null;
    this.duration = 180;
    this.units = "imperial";
    this.position = 0;
    this.busy = false;
    this.play.addEventListener("click", () => this.toggle());
    this.element.addEventListener("click", (event) => {
      if (!this.manifest || event.target.closest(".event-marker")) return;
      const box = this.element.getBoundingClientRect();
      this.seek(((event.clientX - box.left) / box.width) * this.duration);
    });
    this.element.addEventListener("keydown", (event) => {
      if (
        this.manifest &&
        ["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)
      ) {
        event.preventDefault();
        this.seek(seekKey(event.key, this.position, this.duration));
      }
    });
    for (const event of ["timeupdate", "seeking", "seeked", "pause", "play"])
      this.audio.addEventListener(event, () => this.update());
    this.audio.addEventListener("ended", () => {
      this.position = this.duration;
      this.audio.pause();
      this.draw();
    });
    this.audio.addEventListener("error", () => {
      if (this.manifest) this.failure();
    });
    const tick = () => {
      if (!this.audio.paused) this.update();
      requestAnimationFrame(tick);
    };
    requestAnimationFrame(tick);
    this.draw();
  }
  setBusy(busy) {
    this.busy = busy;
    this.draw();
  }
  async toggle() {
    if (!this.manifest || this.busy) return;
    if (!this.audio.paused) {
      this.audio.pause();
      return;
    }
    if (this.position >= this.duration) this.seek(0);
    try {
      await this.audio.play();
      this.update();
    } catch {
      this.failure();
    }
  }
  failure() {
    const position = this.position;
    this.audio.pause();
    this.position = position;
    this.draw();
    showError(this.root.getElementById("main-error"), {
      description: "The replay audio could not be played.",
      action: "Try Play again, or generate a new replay.",
    });
  }
  seek(value) {
    if (!this.manifest) return;
    this.position = clamp(value, this.duration);
    this.audio.currentTime = this.position;
    this.draw();
  }
  update() {
    if (this.manifest) {
      this.position = clamp(this.audio.currentTime, this.duration);
      this.draw();
    }
  }
  unload(duration = this.duration) {
    this.audio.pause();
    this.manifest = null;
    this.audio.removeAttribute("src");
    this.audio.load();
    this.position = 0;
    this.duration = duration;
    this.root.getElementById("markers").replaceChildren();
    this.root.getElementById("stage-bands").replaceChildren();
    this.draw();
  }
  load(manifest, url) {
    this.unload(manifest.target_duration_s);
    this.manifest = manifest;
    this.audio.src = url;
    this.audio.load();
    const bands = this.root.getElementById("stage-bands");
    for (const [a, b, state] of manifest.coarse_states) {
      const span = document.createElement("span");
      span.className = `stage-band stage-${state}`;
      span.style.width = `${((b - a) / this.duration) * 100}%`;
      bands.append(span);
    }
    for (const event of [...manifest.night_events].sort(
      (a, b) => Date.parse(a.night_time) - Date.parse(b.night_time),
    )) {
      const marker = document.createElement("button");
      marker.type = "button";
      marker.className = "event-marker";
      marker.style.left = `${markerPosition(event, this.duration)}%`;
      marker.setAttribute("aria-label", event.label);
      const label = document.createElement("span");
      label.className = "event-label";
      label.textContent = event.label;
      marker.append(label);
      marker.addEventListener("click", (e) => {
        e.stopPropagation();
        this.seek(event.replay_time_s);
      });
      this.root.getElementById("markers").append(marker);
    }
    this.draw();
  }
  draw() {
    this.root.getElementById("position-label").textContent = clock(
      this.position,
    );
    this.root.getElementById("duration-label").textContent = clock(
      this.duration,
    );
    this.root.getElementById("playhead").style.left =
      `${(this.position / this.duration) * 100}%`;
    this.element.setAttribute("aria-valuemax", this.duration);
    this.element.setAttribute("aria-valuenow", this.position);
    this.element.setAttribute("aria-valuetext", clock(this.position));
    this.play.disabled = !this.manifest || this.busy;
    this.play.textContent = this.audio.paused ? "Play" : "Pause";
    this.root.getElementById("state-label").textContent = this.manifest
      ? STATE_NAMES[
          segmentAt(
            this.manifest.coarse_states,
            this.position,
            this.duration,
          )?.[2] || "unknown"
        ]
      : "";
    this.root.getElementById("night-time").textContent = this.manifest
      ? formatNight(
          nightTime(this.manifest, this.position),
          this.manifest.display_timezone,
        )
      : "";
    const values = this.manifest
      ? environmentalValues(
          this.manifest.environmental_windows[
            Math.min(
              this.manifest.environmental_windows.length - 1,
              Math.floor(this.position / 0.5),
            )
          ],
          this.manifest.availability,
          this.units,
        )
      : [];
    this.root.getElementById("environment-values").replaceChildren(
      ...values.map((value) => {
        const span = document.createElement("span");
        span.textContent = value;
        return span;
      }),
    );
  }
}
