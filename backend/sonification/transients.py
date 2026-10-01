"""Movement and brief-awakening blips; rolling one-second density control."""
from datetime import timezone

from backend.domain.events import MOVEMENT_TRANSIENT_THRESHOLD, RESTLESS_MIN_DURATION, RESTLESS_THRESHOLD
from backend.domain.sound import Mapping_Target, Transient_Event
from backend.sonification.contributions import window_spans

MAX_TRANSIENTS_PER_SECOND = 4


def cap_transients(candidates, cap=MAX_TRANSIENTS_PER_SECOND):
    # Prefer strong events, then earlier events. Enforce every rolling interval,
    # including intervals straddling integer-second boundaries.
    accepted = []
    for event in sorted(candidates, key=lambda e: (-e.amplitude, e.onset_s, e.kind)):
        trial = sorted([*accepted, event], key=lambda e: (e.onset_s, e.kind))
        if any(trial[i + cap].onset_s - trial[i].onset_s < 1 for i in range(len(trial) - cap)):
            continue
        accepted = trial
    return tuple(accepted)


def restless_windows(features, states):
    active = [s.value == "restless" for s in states]
    spans = window_spans(features)
    i = 0
    while i < len(features):
        if features.windows[i].movement_intensity <= RESTLESS_THRESHOLD:
            i += 1
            continue
        j = i + 1
        while j < len(features) and features.windows[j].movement_intensity > RESTLESS_THRESHOLD:
            j += 1
        if (spans[j - 1][1] - spans[i][0]) * features.compression_ratio >= RESTLESS_MIN_DURATION.total_seconds():
            active[i:j] = [True] * (j - i)
        i = j
    return tuple(active)


def schedule_transients(features, config, rng):
    candidates = []
    movement = config.metrics["movement"]
    if movement.target is not Mapping_Target.none and movement.sensitivity > 0:
        for w, (start, end) in zip(features.windows, window_spans(features)):
            if w.movement_intensity > MOVEMENT_TRANSIENT_THRESHOLD:
                candidates.append(Transient_Event(start + (end - start) * (0.25 + 0.5 * rng.random()),
                                                  movement.sensitivity * w.movement_intensity, "movement"))
    if features.windows:
        origin = features.windows[0].start_time.astimezone(timezone.utc)
        for start, end in features.brief_awakenings:
            if (end.astimezone(timezone.utc) - start.astimezone(timezone.utc)).total_seconds() < features.compression_ratio:
                onset = (start.astimezone(timezone.utc) - origin).total_seconds() / features.compression_ratio
                candidates.append(Transient_Event(onset, 0.4, "brief_awakening"))
    return cap_transients(candidates)
