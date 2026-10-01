"""Session_Detector: candidate SleepSession discovery, listing and selection.

Requirements 8.5 and 8.6 (dedup and merge, task 9.1); 8.1-8.4, 8.7-8.9 and
8.11 (listing, automatic/user/manual selection, task 9.3). Telemetry
attachment (task 9.4) builds on these functions.

Selection is pure: the functions here take the current selection (anything
with ``session`` and ``kind`` attributes, such as :class:`Selection` or the
persistence layer's ``SelectedSession``) and return the new one. They never
persist anything; the pipeline stores the result.

All datetime comparisons and durations use UTC instants
(``dt.astimezone(timezone.utc)``): datetimes that share one ``ZoneInfo``
compare and subtract by wall time, which is wrong across a DST fall-back.

Determinism: every choice uses a total order over the candidates' content
(never their input position), so the result does not depend on input order,
and :func:`dedup_and_merge` is idempotent (its output is a fixpoint).

Imports only the standard library, ``backend.domain`` and
``backend.processing.timezones`` (Dependency_Rules); never persistence.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone, tzinfo
from typing import Final, Iterable, Literal, Protocol, Sequence
from zoneinfo import ZoneInfo

from backend.domain.errors import (
    INVALID_MANUAL_RANGE,
    NO_SLEEP_SESSION,
    Import_Report,
    User_Error,
    Warning_Item,
)
from backend.domain.session import SleepSession, StageDataAvailability
from backend.domain.stages import Sleep_Stage, Stage_Segment
from backend.domain.timezones import localize
from backend.processing.timezones import DisplayTimezone, elapsed_seconds, to_display

__all__ = [
    "Candidate_Resolution",
    "deduplicate_candidates",
    "merge_overlapping_candidates",
    "dedup_and_merge",
    # Listing and selection (task 9.3)
    "SelectionKind",
    "SELECTION_KINDS",
    "MANUAL_SOURCE",
    "MAX_MANUAL_RANGE",
    "Selection",
    "Session_Listing_Entry",
    "session_key",
    "list_sessions",
    "auto_select",
    "choose_session",
    "parse_manual_time",
    "build_manual_session",
    "define_manual_session",
    "find_containing_session",
    "no_sleep_session_error",
    "resolve_selection",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _u(value: datetime) -> datetime:
    """The UTC instant of an aware datetime."""
    return value.astimezone(timezone.utc)


def _duration(session: SleepSession) -> timedelta:
    """UTC elapsed duration of a session."""
    return _u(session.end_time) - _u(session.start_time)


def _content_key(session: SleepSession) -> tuple:
    """A total order over a candidate's content, used as the final tie-break
    so that choices never depend on input order."""
    hrv = session.session_hrv
    return (
        session.source_file is None,
        session.source_file or "",
        session.source,
        session.log_id is None,
        session.log_id or "",
        _u(session.start_time),
        _u(session.end_time),
        tuple(
            (_u(s.start_time), _u(s.end_time), s.stage.value, s.is_brief_awakening)
            for s in session.stages
        ),
        hrv is None,
        0.0 if hrv is None else float(hrv),
    )


def _dedup_preference(session: SleepSession) -> tuple:
    """Ascending sort key: the first candidate is the one kept (Req 8.5).

    Main sleep first, then ``stages`` data, then the file name that sorts
    first (a missing file name sorts last), then the content key.
    """
    return (
        not session.is_main_sleep,
        session.stage_data_availability != "stages",
        _content_key(session),
    )


def _merge_priority(session: SleepSession) -> tuple:
    """Ascending sort key: earlier candidates own contested time (Req 8.6).

    Main sleep first, then the longer candidate, then the earlier-starting
    one, then the content key.
    """
    return (
        not session.is_main_sleep,
        -_duration(session),
        _u(session.start_time),
        _content_key(session),
    )


def _format(value: datetime, display_tz: tzinfo | None) -> str:
    shown = value.astimezone(display_tz) if display_tz is not None else value
    return shown.isoformat()


# ---------------------------------------------------------------------------
# Deduplication (Requirement 8.5)
# ---------------------------------------------------------------------------


def deduplicate_candidates(
    candidates: Iterable[SleepSession],
) -> tuple[list[SleepSession], int]:
    """Drop duplicate candidates and return ``(kept, discarded_count)``.

    Two candidates are duplicates when they share a (non-``None``) logId or
    have identical start and end instants. Duplication is taken transitively
    (A shares a logId with B, B has C's bounds: all three are one group), so
    no two kept candidates share a logId or bounds. From each group the kept
    candidate is the main-sleep one, then the one with ``stages`` data, then
    the one whose ``source_file`` sorts first. The kept list is ordered by
    start instant, then end instant, then content.
    """
    items = list(candidates)
    parent = list(range(len(items)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    first_by_log: dict[str, int] = {}
    first_by_bounds: dict[tuple[datetime, datetime], int] = {}
    for index, session in enumerate(items):
        if session.log_id is not None:
            union(index, first_by_log.setdefault(session.log_id, index))
        bounds = (_u(session.start_time), _u(session.end_time))
        union(index, first_by_bounds.setdefault(bounds, index))

    groups: dict[int, list[SleepSession]] = {}
    for index, session in enumerate(items):
        groups.setdefault(find(index), []).append(session)

    kept = [min(group, key=_dedup_preference) for group in groups.values()]
    kept.sort(key=lambda s: (_u(s.start_time), _u(s.end_time), _content_key(s)))
    return kept, len(items) - len(kept)


# ---------------------------------------------------------------------------
# Merging (Requirement 8.6)
# ---------------------------------------------------------------------------


def _subtract(
    regions: list[tuple[datetime, datetime]], cut_start: datetime, cut_end: datetime
) -> list[tuple[datetime, datetime]]:
    """Remove ``[cut_start, cut_end)`` from each region (compared in UTC)."""
    cs, ce = _u(cut_start), _u(cut_end)
    result: list[tuple[datetime, datetime]] = []
    for start, end in regions:
        s, e = _u(start), _u(end)
        if ce <= s or cs >= e:
            result.append((start, end))
            continue
        if s < cs:
            result.append((start, cut_start))
        if ce < e:
            result.append((cut_end, end))
    return result


def _clip(segment: Stage_Segment, start: datetime, end: datetime) -> Stage_Segment | None:
    """Clip a segment to ``[start, end)``, reusing its own bounds where possible."""
    new_start = segment.start_time if _u(segment.start_time) >= _u(start) else start
    new_end = segment.end_time if _u(segment.end_time) <= _u(end) else end
    if _u(new_end) <= _u(new_start):
        return None
    if new_start is segment.start_time and new_end is segment.end_time:
        return segment
    return replace(segment, start_time=new_start, end_time=new_end)


def _merge_component(members: Sequence[SleepSession]) -> SleepSession:
    """Merge one connected group of overlapping candidates.

    Members are ranked by :func:`_merge_priority`. Each instant of the union
    takes the Stage_Segments of the highest-ranked member whose interval
    covers it. For two candidates this is exactly Requirement 8.6; for more it
    equals merging pairwise in rank order, where the accumulated union always
    wins (it is main sleep if any earlier member was, and it is longer).
    """
    ranked = sorted(members, key=_merge_priority)
    primary = ranked[0]

    stages: list[Stage_Segment] = []
    for rank, member in enumerate(ranked):
        owned = [(member.start_time, member.end_time)]
        for higher in ranked[:rank]:
            owned = _subtract(owned, higher.start_time, higher.end_time)
            if not owned:
                break
        for region_start, region_end in owned:
            for segment in member.stages:
                clipped = _clip(segment, region_start, region_end)
                if clipped is not None:
                    stages.append(clipped)
    stages.sort(key=lambda s: _u(s.start_time))

    # Bounds: the earliest start / latest end; ties go to the higher rank.
    start = min(ranked, key=lambda s: _u(s.start_time)).start_time
    end = max(ranked, key=lambda s: _u(s.end_time)).end_time

    log_id = next((m.log_id for m in ranked if m.log_id is not None), None)
    session_hrv = next((m.session_hrv for m in ranked if m.session_hrv is not None), None)
    telemetry = tuple(
        sorted(
            dict.fromkeys(p for m in ranked for p in m.telemetry),
            key=lambda p: _u(p.timestamp),
        )
    )

    return SleepSession(
        start_time=start,
        end_time=end,
        source=primary.source,
        stages=tuple(stages),
        telemetry=telemetry,
        log_id=log_id,
        is_main_sleep=any(m.is_main_sleep for m in ranked),
        session_hrv=session_hrv,
        source_file=primary.source_file,
    )


def _merge_warning(
    members: Sequence[SleepSession], merged: SleepSession, display_tz: tzinfo | None
) -> Warning_Item:
    ordered = sorted(members, key=lambda s: (_u(s.start_time), _u(s.end_time), _content_key(s)))
    parts = ", ".join(
        f"{_format(m.start_time, display_tz)} to {_format(m.end_time, display_tz)}"
        for m in ordered
    )
    return Warning_Item(
        description=(
            f"{len(ordered)} overlapping sleep sessions ({parts}) were merged into one "
            f"session from {_format(merged.start_time, display_tz)} "
            f"to {_format(merged.end_time, display_tz)}."
        ),
        subject=merged.source_file,
    )


def merge_overlapping_candidates(
    candidates: Iterable[SleepSession],
    display_tz: tzinfo | None = None,
) -> tuple[list[SleepSession], list[Warning_Item]]:
    """Merge overlapping candidates until none overlap (Requirement 8.6).

    Two candidates overlap when each starts strictly before the other ends;
    touching intervals do not. Candidates are grouped into connected overlap
    components in one sweep (the union of overlapping intervals is itself an
    interval, so this equals repeating pairwise merges to a fixpoint). A
    component of one candidate is returned unchanged; each larger component
    becomes one session spanning the union, flagged main sleep if any member
    was, and adds one warning with the members' and the merged bounds
    (formatted in ``display_tz`` when given).

    Returns ``(sessions, warnings)``, both ordered by start instant.
    """
    ordered = sorted(
        candidates, key=lambda s: (_u(s.start_time), _u(s.end_time), _content_key(s))
    )
    components: list[list[SleepSession]] = []
    component_end: datetime | None = None
    for session in ordered:
        if components and component_end is not None and _u(session.start_time) < component_end:
            components[-1].append(session)
            component_end = max(component_end, _u(session.end_time))
        else:
            components.append([session])
            component_end = _u(session.end_time)

    sessions: list[SleepSession] = []
    warnings: list[Warning_Item] = []
    for members in components:
        if len(members) == 1:
            sessions.append(members[0])
            continue
        merged = _merge_component(members)
        sessions.append(merged)
        warnings.append(_merge_warning(members, merged, display_tz))
    return sessions, warnings


# ---------------------------------------------------------------------------
# Combined operation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Candidate_Resolution:  # noqa: N801 - name follows the spec glossary style
    """Result of :func:`dedup_and_merge`.

    Attributes:
        sessions: Remaining candidates, non-overlapping, ordered by start.
        discarded_duplicates: Number of candidates dropped as duplicates.
        warnings: One warning per merged group (Requirement 8.6).
    """

    sessions: tuple[SleepSession, ...]
    discarded_duplicates: int
    warnings: tuple[Warning_Item, ...]


def dedup_and_merge(
    candidates: Iterable[SleepSession],
    report: Import_Report | None = None,
    display_tz: tzinfo | None = None,
) -> Candidate_Resolution:
    """Deduplicate (Req 8.5), then merge overlaps to a fixpoint (Req 8.6).

    When ``report`` is given, its ``discarded_duplicate_sessions`` count is
    increased by the number of discarded duplicates and the merge warnings are
    appended to its ``warnings``. The result is independent of input order,
    and applying this function to its own ``sessions`` changes nothing.
    """
    kept, discarded = deduplicate_candidates(candidates)
    sessions, warnings = merge_overlapping_candidates(kept, display_tz)
    if report is not None:
        report.discarded_duplicate_sessions += discarded
        report.warnings.extend(warnings)
    return Candidate_Resolution(tuple(sessions), discarded, tuple(warnings))


# ---------------------------------------------------------------------------
# Listing and selection (Requirements 8.1-8.4, 8.7-8.9, 8.11)
# ---------------------------------------------------------------------------

#: How the selected SleepSession became selected. Same values as the
#: persistence layer's ``SelectionKind``, so a :class:`Selection` can be stored
#: with ``MetadataStore.set_selected_session(selection.session, selection.kind)``.
SelectionKind = Literal["auto", "user", "manual"]
SELECTION_KINDS: frozenset[str] = frozenset({"auto", "user", "manual"})

#: ``SleepSession.source`` of a manually defined session.
MANUAL_SOURCE: Final = "manual"

#: Longest accepted manual range (Requirement 8.9).
MAX_MANUAL_RANGE: Final = timedelta(hours=24)


class _SelectionLike(Protocol):
    """Anything with ``session`` and ``kind``: :class:`Selection` or the
    persistence layer's ``SelectedSession``."""

    @property
    def session(self) -> SleepSession: ...

    @property
    def kind(self) -> str: ...


@dataclass(frozen=True)
class Selection:
    """The selected SleepSession and how it became selected.

    ``kind`` is ``"auto"`` (Requirement 8.2/8.3), ``"user"`` (a candidate the
    user chose, 8.4) or ``"manual"`` (a manually defined range, 8.8). Only
    ``"user"`` and ``"manual"`` selections survive later imports (8.11).
    """

    session: SleepSession
    kind: SelectionKind

    def __post_init__(self) -> None:
        if self.kind not in SELECTION_KINDS:
            raise ValueError(f"kind must be one of {sorted(SELECTION_KINDS)}")

    @property
    def is_sticky(self) -> bool:
        """True for user-chosen and manual selections (Requirement 8.11)."""
        return self.kind in ("user", "manual")


def _zone(display_tz: ZoneInfo | DisplayTimezone) -> ZoneInfo:
    return display_tz.zone if isinstance(display_tz, DisplayTimezone) else display_tz


def _utc_text(value: datetime) -> str:
    return _u(value).isoformat().replace("+00:00", "Z")


def session_key(session: SleepSession) -> str:
    """A stable identifier for a listed session: its UTC bounds as an ISO 8601
    interval (``2024-03-01T21:30:00Z/2024-03-02T05:45:00Z``).

    After :func:`dedup_and_merge` no two candidates share bounds (or even
    overlap), so the key identifies one candidate. It does not depend on the
    Display_Timezone or on the offsets the timestamps carry.
    """
    return f"{_utc_text(session.start_time)}/{_utc_text(session.end_time)}"


@dataclass(frozen=True)
class Session_Listing_Entry:  # noqa: N801 - name follows the spec glossary style
    """One row of the session list (Requirement 8.1).

    Attributes:
        key: :func:`session_key` of the session, for :func:`choose_session`.
        session: The candidate SleepSession itself.
        start_time: Start in Display_Timezone.
        end_time: End in Display_Timezone.
        duration_minutes: UTC elapsed duration in whole minutes (truncated).
        stage_data_availability: ``stages``, ``classic`` or ``none``.
        is_main_sleep: The candidate's main-sleep flag.
    """

    key: str
    session: SleepSession
    start_time: datetime
    end_time: datetime
    duration_minutes: int
    stage_data_availability: StageDataAvailability
    is_main_sleep: bool

    @property
    def duration_hours_part(self) -> int:
        return self.duration_minutes // 60

    @property
    def duration_minutes_part(self) -> int:
        return self.duration_minutes % 60

    @property
    def duration_text(self) -> str:
        """Duration in hours and minutes, for example ``7 h 05 min``."""
        return f"{self.duration_hours_part} h {self.duration_minutes_part:02d} min"

    def to_dict(self) -> dict[str, object]:
        """JSON-serializable form (times as ISO 8601 with their display offset)."""
        return {
            "key": self.key,
            "start_time": self.start_time.isoformat(),
            "end_time": self.end_time.isoformat(),
            "duration_hours": self.duration_hours_part,
            "duration_minutes": self.duration_minutes_part,
            "duration_text": self.duration_text,
            "stage_data_availability": self.stage_data_availability,
            "is_main_sleep": self.is_main_sleep,
        }


def _latest_first(sessions: Iterable[SleepSession]) -> list[SleepSession]:
    """Latest start first, then latest end, then content (deterministic)."""
    ordered = sorted(sessions, key=_content_key)
    # A stable sort with reverse=True keeps the content order among equals.
    ordered.sort(key=lambda s: (_u(s.start_time), _u(s.end_time)), reverse=True)
    return ordered


def list_sessions(
    sessions: Iterable[SleepSession],
    display_tz: ZoneInfo | DisplayTimezone,
) -> list[Session_Listing_Entry]:
    """List candidates ordered by start time, latest first (Requirement 8.1).

    ``sessions`` should be the output of :func:`dedup_and_merge` over the
    candidates of all completed imports. Start and end are converted to the
    Display_Timezone; the duration is UTC elapsed time, so it is correct across
    DST transitions. Equal starts are ordered by later end, then by content,
    so the result does not depend on input order.
    """
    zone = _zone(display_tz)
    entries: list[Session_Listing_Entry] = []
    for session in _latest_first(sessions):
        seconds = elapsed_seconds(session.start_time, session.end_time)
        entries.append(
            Session_Listing_Entry(
                key=session_key(session),
                session=session,
                start_time=to_display(session.start_time, zone),
                end_time=to_display(session.end_time, zone),
                duration_minutes=max(0, int(seconds // 60)),
                stage_data_availability=session.stage_data_availability,
                is_main_sleep=session.is_main_sleep,
            )
        )
    return entries


def auto_select(
    sessions: Iterable[SleepSession],
    display_tz: ZoneInfo | DisplayTimezone,
) -> SleepSession | None:
    """Automatic selection (Requirements 8.2, 8.3); ``None`` for no candidates.

    The main-sleep candidate with the latest start instant wins. Without a
    main-sleep candidate, the longest candidate whose end falls on the latest
    end calendar date in the Display_Timezone wins, the later start breaking
    equal durations. Remaining ties go to content, never to input order.
    """
    items = list(sessions)
    if not items:
        return None
    main = [s for s in items if s.is_main_sleep]
    if main:
        return max(main, key=lambda s: (_u(s.start_time), _u(s.end_time), _content_key(s)))

    zone = _zone(display_tz)
    end_dates = {id(s): to_display(s.end_time, zone).date() for s in items}
    latest_date = max(end_dates.values())
    on_latest = [s for s in items if end_dates[id(s)] == latest_date]
    return max(on_latest, key=lambda s: (_duration(s), _u(s.start_time), _content_key(s)))


def choose_session(
    sessions: Iterable[SleepSession],
    choice: str | SleepSession,
) -> Selection:
    """The user chooses a listed candidate (Requirement 8.4).

    ``choice`` is a :func:`session_key` or a SleepSession (matched by its UTC
    bounds). The result replaces any previous selection, including a manual
    one; the caller persists it.

    Raises:
        LookupError: No candidate matches ``choice``.
    """
    key = choice if isinstance(choice, str) else session_key(choice)
    matches = [s for s in sessions if session_key(s) == key]
    if not matches:
        raise LookupError(f"no candidate sleep session matches {key!r}")
    return Selection(min(matches, key=_content_key), "user")


# -- manual ranges (Requirements 8.8, 8.9) -----------------------------------


def parse_manual_time(value: object, display_tz: ZoneInfo | DisplayTimezone) -> datetime:
    """Parse one end of a manual range as an aware instant (Requirement 8.8).

    Accepts a ``datetime`` or an ISO 8601 date-time string (``T`` or space
    separator, seconds optional). A value without an offset is interpreted in
    the Display_Timezone with the DST rules of ``localize`` (ambiguous ->
    earlier instant, nonexistent -> shifted forward); an explicit offset is
    kept. The result is expressed in the Display_Timezone.

    Raises:
        ValueError: ``value`` is missing, blank, date-only or unparseable.
    """
    zone = _zone(display_tz)
    if value is None:
        raise ValueError("missing")
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError("missing")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            raise ValueError("unparseable") from None
        try:
            date.fromisoformat(text)
        except ValueError:
            pass
        else:
            raise ValueError("unparseable")  # date only: no time of day given
    else:
        raise ValueError("unparseable")
    return to_display(localize(parsed, zone).value, zone)


def _invalid_manual_range(
    reason: str, start: object, end: object, zone: ZoneInfo
) -> User_Error:
    return User_Error(
        INVALID_MANUAL_RANGE,
        (
            f"The manual sleep session range was not accepted: {reason}. "
            "A valid range has an end time after the start time and lasts at most 24 hours."
        ),
        (
            f"Enter both a start time and an end time in {zone.key} "
            "(for example 2024-03-01 22:30 and 2024-03-02 06:45), with the end after "
            "the start and no more than 24 hours later."
        ),
        details={
            "reason": reason,
            "start": "" if start is None else str(start),
            "end": "" if end is None else str(end),
            "display_timezone": zone.key,
        },
    )


def build_manual_session(
    start: object,
    end: object,
    display_tz: ZoneInfo | DisplayTimezone,
) -> SleepSession:
    """Create the SleepSession for a manual range (Requirements 8.8, 8.9).

    The session spans ``[start, end]`` in the Display_Timezone, has one
    ``unknown`` Stage_Segment covering the whole range, no main-sleep flag,
    and ``source`` :data:`MANUAL_SOURCE`.

    Raises:
        User_Error: ``INVALID_MANUAL_RANGE`` when either time is missing or
            unparseable, the end is not after the start, or the range is
            longer than 24 hours (compared as UTC instants). Nothing is
            changed, so the caller keeps its current selection.
    """
    zone = _zone(display_tz)
    parsed: list[datetime] = []
    for label, raw in (("start", start), ("end", end)):
        try:
            parsed.append(parse_manual_time(raw, zone))
        except ValueError as exc:
            problem = "is missing" if str(exc) == "missing" else "could not be read as a date and time"
            raise _invalid_manual_range(f"the {label} time {problem}", start, end, zone) from None
    start_dt, end_dt = parsed
    seconds = elapsed_seconds(start_dt, end_dt)
    if seconds <= 0:
        raise _invalid_manual_range("the end time is not after the start time", start, end, zone)
    if seconds > MAX_MANUAL_RANGE.total_seconds():
        raise _invalid_manual_range("the range is longer than 24 hours", start, end, zone)
    return SleepSession(
        start_time=start_dt,
        end_time=end_dt,
        source=MANUAL_SOURCE,
        stages=(Stage_Segment(start_dt, end_dt, Sleep_Stage.unknown),),
        is_main_sleep=False,
    )


def define_manual_session(
    start: object,
    end: object,
    display_tz: ZoneInfo | DisplayTimezone,
) -> Selection:
    """The user defines a manual range (Requirement 8.8): returns the new
    ``"manual"`` Selection, replacing any previous selection.

    Raises:
        User_Error: ``INVALID_MANUAL_RANGE`` (see :func:`build_manual_session`);
            the caller then keeps the current selection (Requirement 8.9).
    """
    return Selection(build_manual_session(start, end, display_tz), "manual")


# -- import completion (Requirements 8.2, 8.3, 8.7, 8.11) --------------------


def find_containing_session(
    sessions: Iterable[SleepSession], target: SleepSession
) -> SleepSession | None:
    """The candidate whose UTC interval contains ``target``'s, or ``None``.

    An exact bounds match wins, then the shortest containing candidate, then
    content. After :func:`dedup_and_merge` at most one candidate can contain
    a positive-length interval, so this is the merged session that absorbed a
    previously chosen candidate (Requirement 8.11).
    """
    t_start, t_end = _u(target.start_time), _u(target.end_time)
    containing = [
        s for s in sessions if _u(s.start_time) <= t_start and t_end <= _u(s.end_time)
    ]
    if not containing:
        return None
    return min(
        containing,
        key=lambda s: (
            (_u(s.start_time), _u(s.end_time)) != (t_start, t_end),
            _duration(s),
            _content_key(s),
        ),
    )


def no_sleep_session_error() -> User_Error:
    """``NO_SLEEP_SESSION``: no sleep log anywhere; offers manual definition
    and states that the telemetry was kept (Requirement 8.7)."""
    return User_Error(
        NO_SLEEP_SESSION,
        (
            "No sleep log was found in any import, so no sleep session could be selected. "
            "The imported telemetry has been kept."
        ),
        (
            "Import a Fitbit export that contains a sleep log for the night, or define the "
            "sleep session manually by entering its start time and end time "
            "(at most 24 hours apart)."
        ),
        details={"manual_definition": "available"},
    )


def resolve_selection(
    sessions: Iterable[SleepSession],
    current: _SelectionLike | None,
    display_tz: ZoneInfo | DisplayTimezone,
) -> Selection:
    """The selection after an import completes.

    ``sessions`` is the :func:`dedup_and_merge` output over all completed
    imports; ``current`` is the selection before the import (``None`` if none).

    - A manual selection is kept unchanged (Requirement 8.11); manual sessions
      are not candidates, so they are never merged.
    - Otherwise, with no candidates, ``NO_SLEEP_SESSION`` is raised
      (Requirement 8.7); the caller keeps the telemetry.
    - A user-chosen selection is kept, following the candidate that contains
      it when a merge enlarged it (Requirement 8.11). If no candidate contains
      it any more, the previous session stays selected unchanged.
    - Otherwise (nothing selected, or an automatic selection) the automatic
      rule picks again (Requirements 8.2, 8.3).

    Raises:
        User_Error: ``NO_SLEEP_SESSION`` as described above.
    """
    if current is not None and current.kind == "manual":
        return Selection(current.session, "manual")
    items = list(sessions)
    if not items:
        raise no_sleep_session_error()
    if current is not None and current.kind == "user":
        follow = find_containing_session(items, current.session)
        return Selection(current.session if follow is None else follow, "user")
    chosen = auto_select(items, display_tz)
    assert chosen is not None  # items is non-empty
    return Selection(chosen, "auto")
