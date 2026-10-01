"""Input_Fingerprint: SHA-256 of a SleepSession's normalized inputs.

Requirement 9.5 (and the Input_Fingerprint glossary entry). The fingerprint
covers the session bounds, every TelemetryPoint, and every Stage_Segment of
the selected SleepSession. It is deterministic and independent of

* the input order of TelemetryPoints and Stage_Segments (the records are
  serialized and then sorted as a multiset, so duplicates still count), and
* the UTC offsets / tzinfo the timestamps carry, and so of the
  Display_Timezone (every instant is hashed as its UTC representation).

Values are hashed exactly as stored (source value and source unit, Requirement
1.4), using ``float.hex`` so no decimal rounding is involved.

Imports only the standard library and ``backend.domain`` (Requirement 17.2).
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Iterable

from backend.domain.session import SleepSession
from backend.domain.stages import Stage_Segment
from backend.domain.telemetry import TelemetryPoint

__all__ = ["FINGERPRINT_VERSION", "input_fingerprint", "fingerprint_inputs"]

#: Bumped whenever the canonical serialization changes.
FINGERPRINT_VERSION = 1


def _utc(dt: datetime) -> str:
    if not isinstance(dt, datetime) or dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError(f"timestamps must be timezone-aware datetimes, got {dt!r}")
    return dt.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _name(value: object) -> str:
    """Stable string for enum members (their value) and plain strings."""
    return str(getattr(value, "value", value))


def _number(value: object) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return repr(value)
    return float(value).hex()


def _point_record(point: TelemetryPoint) -> list[str]:
    return [
        _utc(point.timestamp),
        str(point.source),
        _name(point.metric),
        _number(point.value),
        str(point.unit),
    ]


def _segment_record(segment: Stage_Segment) -> list[str]:
    return [
        _utc(segment.start_time),
        _utc(segment.end_time),
        _name(segment.stage),
        "brief" if segment.is_brief_awakening else "",
    ]


def fingerprint_inputs(
    start_time: datetime,
    end_time: datetime,
    telemetry: Iterable[TelemetryPoint],
    stages: Iterable[Stage_Segment],
) -> str:
    """Input_Fingerprint (lower-case hex SHA-256) of explicit session inputs."""
    payload = {
        "version": FINGERPRINT_VERSION,
        "session": [_utc(start_time), _utc(end_time)],
        "telemetry": sorted(_point_record(p) for p in telemetry),
        "stages": sorted(_segment_record(s) for s in stages),
    }
    encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def input_fingerprint(session: SleepSession) -> str:
    """Input_Fingerprint of ``session``: SHA-256 over its UTC bounds and its
    canonically sorted, UTC-normalized TelemetryPoints and Stage_Segments."""
    return fingerprint_inputs(
        session.start_time, session.end_time, session.telemetry, session.stages
    )
