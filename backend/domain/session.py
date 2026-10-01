"""SleepSession: a normalized sleep period with its stages and telemetry.

Requirements 2.1, 2.4, 2.5, 2.6, 2.10, 3.8, 3.11, 4.3, 8.1. Imports only the
standard library and other ``backend.domain`` modules.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import cached_property
from typing import TYPE_CHECKING, Literal

from backend.domain.stages import Sleep_Stage, Stage_Segment

if TYPE_CHECKING:  # avoid import-time coupling with telemetry.py
    from backend.domain.telemetry import TelemetryPoint

__all__ = ["SleepSession", "StageDataAvailability", "SLEEP_STAGES_EXCLUDED_FROM_ONSET"]

StageDataAvailability = Literal["stages", "classic", "none"]

# Stages that do not count toward sleep onset / final wake (Requirement 2.5, 2.6).
SLEEP_STAGES_EXCLUDED_FROM_ONSET: frozenset[Sleep_Stage] = frozenset(
    {Sleep_Stage.awake, Sleep_Stage.unknown}
)

# Sleep_Stages that only stage-style (light/deep/rem) or classic-style
# (asleep/restless) logs produce; ``awake`` comes from both styles.
_STAGE_STYLE: frozenset[Sleep_Stage] = frozenset(
    {Sleep_Stage.light, Sleep_Stage.deep, Sleep_Stage.rem}
)
_CLASSIC_STYLE: frozenset[Sleep_Stage] = frozenset(
    {Sleep_Stage.asleep, Sleep_Stage.restless}
)


@dataclass(frozen=True)
class SleepSession:
    """A normalized sleep period (Requirement 2.1).

    ``stages`` is ordered by start_time and non-overlapping, each segment
    within ``[start_time, end_time]`` (Requirement 2.2; enforced by the
    producers via ``build_stage_segments``). ``telemetry`` holds the points
    with ``start_time <= timestamp <= end_time`` (Requirement 2.1, 8.10).
    Lists passed for ``stages`` or ``telemetry`` are stored as tuples.
    """

    start_time: datetime
    end_time: datetime
    source: str
    stages: tuple[Stage_Segment, ...] = ()
    telemetry: tuple[TelemetryPoint, ...] = ()
    # Provenance for the Session_Detector (Requirement 3.8, 3.11, 4.3, 8.5).
    log_id: str | None = None
    is_main_sleep: bool = False
    session_hrv: float | None = None  # Session_HRV in ms
    source_file: str | None = None  # originating file name, for dedup tie-breaks and warnings

    # Internal lookup index for stage_at(); not part of equality or repr.
    _stage_starts: tuple[datetime, ...] = field(
        init=False, repr=False, compare=False, default=()
    )

    def __post_init__(self) -> None:
        if not isinstance(self.stages, tuple):
            object.__setattr__(self, "stages", tuple(self.stages))
        if not isinstance(self.telemetry, tuple):
            object.__setattr__(self, "telemetry", tuple(self.telemetry))
        object.__setattr__(
            self, "_stage_starts", tuple(s.start_time.astimezone(timezone.utc) for s in self.stages)
        )

    # --- Derived values -------------------------------------------------

    @cached_property
    def _sleep_segments(self) -> tuple[Stage_Segment, ...]:
        return tuple(
            s for s in self.stages if s.stage not in SLEEP_STAGES_EXCLUDED_FROM_ONSET
        )

    @property
    def sleep_onset_time(self) -> datetime | None:
        """Start of the first segment that is neither awake nor unknown.

        None when no such segment exists (Requirement 2.5, 2.10).
        """
        segments = self._sleep_segments
        if not segments:
            return None
        return min((s.start_time for s in segments), key=lambda t: t.astimezone(timezone.utc))

    @property
    def final_wake_time(self) -> datetime | None:
        """End of the last segment that is neither awake nor unknown.

        None when no such segment exists (Requirement 2.6, 2.10).
        """
        segments = self._sleep_segments
        if not segments:
            return None
        return max((s.end_time for s in segments), key=lambda t: t.astimezone(timezone.utc))

    @property
    def has_stage_data(self) -> bool:
        """True iff any Stage_Segment has a stage other than unknown (Stage_Data)."""
        return any(s.stage is not Sleep_Stage.unknown for s in self.stages)

    @property
    def stage_data_availability(self) -> StageDataAvailability:
        """Stage-data availability label for session listing (Requirement 8.1).

        ``stages`` when any light/deep/rem segment exists; otherwise
        ``classic`` when any Stage_Data exists (asleep/restless, or only
        awake segments, which carry no stage-style detail); otherwise ``none``.
        """
        present = {s.stage for s in self.stages}
        if present & _STAGE_STYLE:
            return "stages"
        if present & _CLASSIC_STYLE or Sleep_Stage.awake in present:
            return "classic"
        return "none"

    def stage_at(self, instant: datetime) -> Sleep_Stage:
        """Sleep_Stage of the half-open segment containing ``instant``.

        Instants covered by no Stage_Segment, including every instant of a
        session without stages and instants outside the session, are
        ``unknown`` (Requirement 2.4). ``instant`` must be timezone-aware.
        """
        idx = bisect_right(self._stage_starts, instant.astimezone(timezone.utc)) - 1
        if idx >= 0:
            segment = self.stages[idx]
            if segment.contains(instant):
                return segment.stage
        return Sleep_Stage.unknown
