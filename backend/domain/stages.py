"""Sleep stages, the Stage_Name_Mapping, and Stage_Segment.

Requirements 2.2, 2.3, 2.4, 3.4, 3.5, 12.6. Imports nothing outside the
standard library, so every Importer and processing component can use it.
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from types import MappingProxyType
from typing import Iterable, Mapping

__all__ = [
    "Sleep_Stage",
    "STAGE_NAME_MAPPING",
    "map_stage_name",
    "DOMINANT_STAGE_ORDER",
    "Stage_Segment",
    "Stage_Interval",
    "build_stage_segments",
]


class Sleep_Stage(str, Enum):
    """Normalized sleep stage (Glossary: Sleep_Stage)."""

    awake = "awake"
    light = "light"
    deep = "deep"
    rem = "rem"
    asleep = "asleep"
    restless = "restless"
    unknown = "unknown"


# Stage_Name_Mapping (Requirement 2.3, 3.5). Keys are lower-case; lookups trim
# and lower-case the source name first. Any other name maps to unknown.
STAGE_NAME_MAPPING: Mapping[str, Sleep_Stage] = MappingProxyType(
    {
        "wake": Sleep_Stage.awake,
        "awake": Sleep_Stage.awake,
        "light": Sleep_Stage.light,
        "core": Sleep_Stage.light,
        "deep": Sleep_Stage.deep,
        "rem": Sleep_Stage.rem,
        "asleep": Sleep_Stage.asleep,
        "restless": Sleep_Stage.restless,
    }
)


def map_stage_name(name: object) -> Sleep_Stage:
    """Map a source stage name to a Sleep_Stage.

    Matching is case-insensitive after removing leading and trailing
    whitespace (Requirement 2.3). Names absent from the mapping, and
    non-string values, map to ``Sleep_Stage.unknown`` (Requirement 3.7).
    """
    if not isinstance(name, str):
        return Sleep_Stage.unknown
    return STAGE_NAME_MAPPING.get(name.strip().casefold(), Sleep_Stage.unknown)


# Fixed tie-break order for the dominant stage of a Feature_Window
# (Requirement 12.6): on equal fractions, the earlier stage in this order wins.
DOMINANT_STAGE_ORDER: tuple[Sleep_Stage, ...] = (
    Sleep_Stage.awake,
    Sleep_Stage.rem,
    Sleep_Stage.light,
    Sleep_Stage.deep,
    Sleep_Stage.asleep,
    Sleep_Stage.restless,
    Sleep_Stage.unknown,
)


@dataclass(frozen=True)
class Stage_Segment:
    """A time interval ``[start_time, end_time)`` with one Sleep_Stage.

    Both timestamps are timezone-aware and ``end_time > start_time``
    (Requirement 2.2). ``is_brief_awakening`` marks awake segments that came
    from Fitbit ``levels.shortData`` (Brief_Awakening, Requirement 3.4).
    """

    start_time: datetime
    end_time: datetime
    stage: Sleep_Stage
    is_brief_awakening: bool = False

    def __post_init__(self) -> None:
        if self.start_time.tzinfo is None or self.start_time.utcoffset() is None:
            raise ValueError("Stage_Segment.start_time must be timezone-aware")
        if self.end_time.tzinfo is None or self.end_time.utcoffset() is None:
            raise ValueError("Stage_Segment.end_time must be timezone-aware")
        if self.end_time.astimezone(timezone.utc) <= self.start_time.astimezone(timezone.utc):
            raise ValueError("Stage_Segment.end_time must be later than start_time")
        if not isinstance(self.stage, Sleep_Stage):
            raise TypeError("Stage_Segment.stage must be a Sleep_Stage")

    def contains(self, instant: datetime) -> bool:
        """True when ``start_time <= instant < end_time`` (half-open)."""
        return self.start_time.astimezone(timezone.utc) <= instant.astimezone(timezone.utc) < self.end_time.astimezone(timezone.utc)


@dataclass(frozen=True)
class Stage_Interval:
    """A raw source stage interval, before normalization.

    Unlike Stage_Segment it may have a zero or negative duration and may lie
    partly or wholly outside the session; ``build_stage_segments`` clips and
    discards such intervals (Requirement 2.9). ``is_brief_awakening`` marks
    intervals from Fitbit ``levels.shortData`` (Requirement 3.4).
    """

    start_time: datetime
    end_time: datetime
    stage: Sleep_Stage
    is_brief_awakening: bool = False


def _require_aware(value: datetime, name: str) -> None:
    if not isinstance(value, datetime):
        raise TypeError(f"{name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


def build_stage_segments(
    intervals: Iterable[Stage_Interval],
    session_start: datetime,
    session_end: datetime,
) -> list[Stage_Segment]:
    """Normalize raw stage intervals into ordered, non-overlapping segments.

    - Every interval is clipped to ``[session_start, session_end]``; intervals
      whose clipped duration is 0 seconds or less are discarded (Req. 2.9).
    - Where two intervals overlap, the overlap goes to the Brief_Awakening if
      exactly one of them is one; otherwise to the later-starting interval,
      and for equal starts to the one later in source (list) order (Req. 2.8).
      This pairwise rule is the total order
      ``(is_brief_awakening, start_time, source_index)``: the highest key wins.
    - The losing interval is split, so its parts before and after the overlap
      stay separate segments with their original stage (Req. 2.8, 3.3).
    - The result is ordered by start_time, and every segment has
      ``end_time > start_time`` and starts at or after the preceding end
      (Req. 2.2). Adjacent equal-stage segments from different source
      intervals are not merged.

    Output timestamps reuse the input datetime objects: for an instant shared
    by several inputs (possibly with different UTC offsets), the first
    occurrence among the clipped starts and ends in source order is used, so
    the output is deterministic for a given input.

    Raises ValueError for naive timestamps. Returns ``[]`` when
    ``session_end <= session_start`` or no interval survives clipping.
    """
    _require_aware(session_start, "session_start")
    _require_aware(session_end, "session_end")
    utc = lambda t: t.astimezone(timezone.utc)
    if utc(session_end) <= utc(session_start):
        return []

    # Clip, discard empty, and remember source order.
    clipped: list[tuple[int, datetime, datetime, Stage_Interval]] = []
    for index, interval in enumerate(intervals):
        _require_aware(interval.start_time, "Stage_Interval.start_time")
        _require_aware(interval.end_time, "Stage_Interval.end_time")
        start = max(interval.start_time, session_start, key=utc)
        end = min(interval.end_time, session_end, key=utc)
        if utc(end) > utc(start):
            clipped.append((index, start, end, interval))
    if not clipped:
        return []

    # Elementary boundaries, deduplicated by instant (first occurrence kept).
    boundary_by_instant: dict[datetime, datetime] = {}
    for _, start, end, _ in clipped:
        boundary_by_instant.setdefault(utc(start), start)
        boundary_by_instant.setdefault(utc(end), end)
    boundaries = sorted(boundary_by_instant.values(), key=utc)

    # Sweep the elementary slices, keeping active intervals in a max-heap on
    # (is_brief_awakening, start_time, source_index); heapq is a min-heap, so
    # the key is negated. Expired intervals are removed lazily from the top.
    # The rank in `order` (sorted by start, then source index) encodes the
    # "later start, then later source order" tie-break.
    order = sorted(range(len(clipped)), key=lambda i: (utc(clipped[i][1]), clipped[i][0]))
    heap: list[tuple[int, int, int]] = []
    next_rank = 0

    # Pieces: [winner position in `clipped`, piece start, piece end].
    pieces: list[list] = []
    for slice_start, slice_end in zip(boundaries, boundaries[1:]):
        while next_rank < len(order) and utc(clipped[order[next_rank]][1]) <= utc(slice_start):
            pos = order[next_rank]
            is_brief = bool(clipped[pos][3].is_brief_awakening)
            heapq.heappush(heap, (-int(is_brief), -next_rank, pos))
            next_rank += 1
        while heap and utc(clipped[heap[0][2]][2]) <= utc(slice_start):
            heapq.heappop(heap)
        if not heap:
            continue  # Uncovered slice: stays unknown (Requirement 2.4).
        winner = heap[0][2]
        if pieces and pieces[-1][0] == winner and utc(pieces[-1][2]) == utc(slice_start):
            pieces[-1][2] = slice_end  # Same source interval continues.
        else:
            pieces.append([winner, slice_start, slice_end])

    segments: list[Stage_Segment] = []
    for winner, piece_start, piece_end in pieces:
        interval = clipped[winner][3]
        segments.append(
            Stage_Segment(
                start_time=piece_start,
                end_time=piece_end,
                stage=interval.stage,
                is_brief_awakening=bool(interval.is_brief_awakening),
            )
        )
    return segments
