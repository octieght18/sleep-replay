"""Version and rights for original, procedurally generated nature audio."""

MUSIC_VERSION = "0.2.0"
NATURE_VERSION = "0.3.0"


def replay_version(config):
    return NATURE_VERSION if config.sound_style == "nature" else MUSIC_VERSION


def nature_provenance():
    return {
        "license": "CC0-1.0",
        "source": "procedural",
        "generator": "sleep-replay-nature-v1",
        "layers": ["surf", "wind", "rain", "leaves", "rumble"],
    }
