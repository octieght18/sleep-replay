"""Sensitivity-weighted combination and a chronological Lipschitz projection."""
from backend.domain.sound import Breakpoint, Mapping_Target, PARAMETER_RATE_LIMITS, Parameter_Trajectory, SOUND_PARAMETERS
from backend.sonification.contributions import window_spans
from backend.sonification.presets import RESTLESS_TEXTURE_BOOST


def rate_limit(points, rate):
    result = [points[0]]
    for point in points[1:]:
        last = result[-1]
        delta = rate * (point.t - last.t)
        value = min(last.value + delta, max(last.value - delta, point.value))
        result.append(Breakpoint(point.t, min(1.0, max(0.0, value))))
    return Parameter_Trajectory(tuple(result))


def combine(contributions, config, features, restless, duration):
    times = {0.0, float(duration)}
    times.update(p.t for trajectory in contributions.values() for p in trajectory.breakpoints if p.t <= duration)
    windows = window_spans(features)
    times.update((a + b) / 2 for a, b in windows)
    times = sorted(times)
    movement = config.metrics["movement"]
    sensitivity = 0 if movement.target is Mapping_Target.none else movement.sensitivity
    boost = Parameter_Trajectory(tuple([Breakpoint(0, RESTLESS_TEXTURE_BOOST * sensitivity if restless and restless[0] else 0)] +
        [Breakpoint((a + b) / 2, RESTLESS_TEXTURE_BOOST * sensitivity if active else 0)
         for (a, b), active in zip(windows, restless)] +
        [Breakpoint(duration, RESTLESS_TEXTURE_BOOST * sensitivity if restless and restless[-1] else 0)]))
    result = {}
    for parameter in SOUND_PARAMETERS:
        weighted = [(contributions[key], m.sensitivity) for key, m in config.metrics.items()
                    if m.target is parameter and m.sensitivity > 0]
        total = sum(weight for _, weight in weighted)
        points = []
        for t in times:
            value = sum(trajectory.value_at(t) * weight for trajectory, weight in weighted) / total if total else 0.5
            if parameter in (Mapping_Target.texture_density, Mapping_Target.transient_density):
                value = min(1.0, value + boost.value_at(t))
            points.append(Breakpoint(t, value))
        result[parameter] = rate_limit(points, PARAMETER_RATE_LIMITS[parameter])
    return result
