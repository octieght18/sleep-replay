"""Cache by normalized session contents, effective sound settings, and version."""

import hashlib
import json
from pathlib import Path

from backend.domain.mapping import mapping_dict
from backend.sonification.generation import REPLAY_VERSION
from backend.sonification.manifest import parse_manifest


def cache_key(fingerprint, config, version=REPLAY_VERSION):
    value = dict(
        input_fingerprint=fingerprint,
        mapping_config=mapping_dict(config),
        version=version,
    )
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class ReplayCache:
    def __init__(self, directory, store):
        self.directory, self.store = Path(directory).resolve(), store

    def paths(self, record):
        wav = (self.directory / record.wav_file).resolve()
        manifest = (self.directory / record.metadata.get("manifest_file", "")).resolve()
        if (
            not wav.is_relative_to(self.directory)
            or not manifest.is_relative_to(self.directory)
            or not wav.is_file()
            or not manifest.is_file()
        ):
            return None
        return wav, manifest

    def find(self, fingerprint, config, display_timezone=None):
        expected = cache_key(fingerprint, config)
        for record in reversed(self.store.list_replays()):
            data = record.metadata
            if (
                data.get("input_fingerprint") != fingerprint
                or data.get("version") != REPLAY_VERSION
                or data.get("mapping_config") != mapping_dict(config)
            ):
                continue
            paths = self.paths(record)
            if paths:
                try:
                    manifest = parse_manifest(paths[1].read_bytes())
                    if cache_key(
                        manifest.input_fingerprint,
                        manifest.mapping_config,
                        manifest.version,
                    ) == expected and (
                        display_timezone is None
                        or manifest.display_timezone == display_timezone
                    ):
                        return record
                except (OSError, ValueError):
                    pass
        return None
