"""Slow event swells with inward clamping at the replay endpoints."""
from backend.domain.events import Event_Type
from backend.domain.sound import Onset_Gesture


def schedule_gestures(events, duration):
    gestures = []
    for event in events:
        if event.type in (Event_Type.sleep_onset, Event_Type.awakening):
            start = min(duration - 2.0, max(0.0, event.replay_time_s - 1.0))
            gestures.append(Onset_Gesture(event.type.name, start, start + 2.0))
    return tuple(gestures)
