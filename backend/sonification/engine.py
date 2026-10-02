"""Pure, deterministic conversion of processed features into an audio plan."""
import random

from backend.domain.mapping import TARGET_DURATIONS, validate_mapping, validate_seed
from backend.domain.sound import Render_Plan
from backend.sonification.combine import combine
from backend.sonification.contributions import contribution_trajectory
from backend.sonification.gestures import schedule_gestures
from backend.sonification.normalize import normalization_spans
from backend.sonification.nature import nature_controls
from backend.sonification.presets import preset_plan
from backend.sonification.transients import restless_windows, schedule_transients


def build_render_plan(features, coarse_states, night_events, config, target_duration, seed):
    validate_mapping(config)
    validate_seed(seed)
    if type(target_duration) is not int or target_duration not in TARGET_DURATIONS:
        raise ValueError("render target duration must be one of the five supported integer durations")
    if len(coarse_states) != len(features) or not features.windows:
        raise ValueError("one coarse state per nonempty feature window is required")
    rng = random.Random(f"{seed}:engine")
    spans = normalization_spans(features)
    contributions = {key: contribution_trajectory(features, key, mapping, spans.get(key), target_duration)
                     for key, mapping in config.metrics.items()}
    return Render_Plan(target_duration, combine(contributions, config, features,
                       restless_windows(features, coarse_states), target_duration),
                       schedule_transients(features, config, rng), schedule_gestures(night_events, target_duration),
                       preset_plan(features, coarse_states), seed, sound_style=config.sound_style,
                       nature_controls=nature_controls(features, config, target_duration) if config.sound_style == "nature" else {})
