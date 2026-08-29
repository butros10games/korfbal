"""Payload builders for match event timelines.

These are *not* DRF serializers; they are lightweight dict payload builders used
by korfbal-web for match event timelines and graphs.

Keeping these helpers out of `views.py` significantly reduces file size and
improves testability.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from django.db import models

from apps.game_tracker.models import (
    MatchData,
    MatchPart,
    Pause,
    PlayerChange,
    PlayerGroup,
    PossessionChange,
    Shot,
    Timeout,
)
from apps.game_tracker.services.match_events import (
    event_root_ids,
    event_root_metadata,
    logical_event_id,
)


PART_ONE = 1
PART_TWO = 2


@dataclass(frozen=True, slots=True)
class _PauseInterval:
    start: datetime
    end: datetime | None
    active: bool


@dataclass(frozen=True, slots=True)
class _TimeoutTimelineData:
    timeout_id: str
    team_id: str | None


@dataclass(frozen=True, slots=True)
class MatchTimelineContext:
    """Relations shared by timeline serializers, loaded in bounded queries."""

    match_parts: tuple[MatchPart, ...]
    pauses: tuple[Pause, ...]
    pause_intervals: tuple[_PauseInterval, ...]
    timeouts_by_pause: dict[str, _TimeoutTimelineData]
    player_team_ids: dict[str, str]
    event_sequences: dict[tuple[str, str], int]
    logical_event_ids: dict[tuple[str, str], str]


def load_match_timeline_context(match_data: MatchData) -> MatchTimelineContext:
    """Load the stable relation context used by event and shot projections."""
    match_parts = tuple(
        MatchPart.objects
        .filter(match_data=match_data)
        .order_by("part_number", "start_time")
        .fetch_mode(models.FETCH_RAISE)
    )
    pauses = tuple(
        Pause.objects
        .select_related("match_part")
        .only(
            "id_uuid",
            "start_time",
            "end_time",
            "active",
            "match_part__id_uuid",
            "match_part__start_time",
            "match_part__part_number",
        )
        .filter(match_data=match_data)
        .order_by("start_time")
        .fetch_mode(models.FETCH_RAISE)
    )
    pause_intervals = tuple(
        _PauseInterval(
            start=pause.start_time,
            end=pause.end_time,
            active=pause.active,
        )
        for pause in pauses
        if pause.start_time is not None
    )

    timeouts_by_pause = {
        str(pause_id): _TimeoutTimelineData(
            timeout_id=str(timeout_id),
            team_id=str(team_id) if team_id is not None else None,
        )
        for pause_id, timeout_id, team_id in Timeout.objects.filter(
            match_data=match_data,
            pause_id__isnull=False,
        ).values_list("pause_id", "id_uuid", "team_id")
    }
    player_team_ids = {
        str(player_id): str(team_id)
        for player_id, team_id in PlayerGroup.objects.filter(
            match_data=match_data,
            players__id_uuid__isnull=False,
        ).values_list("players__id_uuid", "team_id")
    }
    event_sequences, logical_event_ids = event_root_metadata(match_data)
    return MatchTimelineContext(
        match_parts=match_parts,
        pauses=pauses,
        pause_intervals=pause_intervals,
        timeouts_by_pause=timeouts_by_pause,
        player_team_ids=player_team_ids,
        event_sequences=event_sequences,
        logical_event_ids=logical_event_ids,
    )


def _intermission_label_for_time(
    match_data: MatchData,
    event_time: datetime,
    *,
    context: MatchTimelineContext | None = None,
) -> str:
    """Return a human label for events that happened between match parts.

    We intentionally keep this as a string label (instead of forcing an artificial
    part-relative minute) so the frontend doesn't show it as added time ("30+X")
    for the previous part.

    """
    if context is None:
        previous_part = (
            MatchPart.objects
            .filter(
                match_data=match_data,
                end_time__isnull=False,
                end_time__lte=event_time,
            )
            .order_by("-part_number", "-end_time")
            .first()
        )
        next_part = (
            MatchPart.objects
            .filter(
                match_data=match_data,
                start_time__gte=event_time,
            )
            .order_by("part_number", "start_time")
            .first()
        )
    else:
        previous_part = next(
            (
                part
                for part in reversed(context.match_parts)
                if part.end_time is not None and part.end_time <= event_time
            ),
            None,
        )
        next_part = next(
            (part for part in context.match_parts if part.start_time >= event_time),
            None,
        )

    # If this event happened between part 1 and part 2 (or part 2 hasn't started
    # yet, so `next_part` is unknown), treat it as half-time.
    if (
        previous_part
        and previous_part.part_number == PART_ONE
        and (next_part is None or next_part.part_number == PART_TWO)
    ):
        return "Rust"

    return "Pauze"


def _event_time_key(event: object) -> datetime:
    value = getattr(event, "time", None)
    if value is not None:
        return value
    value = getattr(event, "start_time", None)
    if value is not None:
        return value
    return datetime.min.replace(tzinfo=UTC)


def _source_key(event: object) -> tuple[str, str] | None:
    if isinstance(event, Shot):
        return "shot", str(event.id_uuid)
    if isinstance(event, PlayerChange):
        return "player_change", str(event.id_uuid)
    if isinstance(event, PossessionChange):
        return "possession_change", str(event.id_uuid)
    if isinstance(event, Pause):
        return "pause", str(event.id_uuid)
    return None


def _ordered_sequence(
    event: object,
    sequences: dict[tuple[str, str], int],
    timeout_ids_by_pause: dict[str, str],
) -> int | None:
    key = _source_key(event)
    candidates = [sequences[key]] if key is not None and key in sequences else []
    if isinstance(event, Pause):
        timeout_id = timeout_ids_by_pause.get(str(event.id_uuid))
        timeout_key = ("timeout", timeout_id) if timeout_id else None
        if timeout_key is not None and timeout_key in sequences:
            candidates.append(sequences[timeout_key])
    return max(candidates, default=None)


def _logical_source_key(
    event: object,
    timeout_ids_by_pause: dict[str, str],
) -> tuple[str, str] | None:
    """Return the one public logical root for a projected timeline item."""
    if isinstance(event, Pause):
        timeout_id = timeout_ids_by_pause.get(str(event.id_uuid))
        if timeout_id is not None:
            return "timeout", timeout_id
    return _source_key(event)


def _time_in_minutes(
    *,
    match_data: MatchData,
    match_part: MatchPart,
    event_time: datetime,
    context: MatchTimelineContext | None = None,
) -> str:
    match_part_start = match_part.start_time
    match_part_number = match_part.part_number
    if context is None:
        pause_intervals = (
            Pause.objects
            .filter(
                match_data=match_data,
                start_time__lt=event_time,
                start_time__gte=match_part_start,
            )
            .filter(
                models.Q(active=True) | models.Q(end_time__isnull=False),
            )
            .values_list("start_time", "end_time")
        )
    else:
        pause_intervals = (
            (interval.start, interval.end)
            for interval in context.pause_intervals
            if interval.start < event_time
            and interval.start >= match_part_start
            and (interval.active or interval.end is not None)
        )
    pause_time = sum(
        (
            min(end_time or event_time, event_time) - start_time
            for start_time, end_time in pause_intervals
            if end_time is None or end_time > start_time
        ),
        timedelta(0),
    )
    pause_time_seconds = pause_time.total_seconds()

    time_in_minutes_value = round(
        (
            (event_time - match_part_start).total_seconds()
            + ((match_part_number - 1) * int(match_data.part_length))
            - pause_time_seconds
        )
        / 60,
    )

    left_over = time_in_minutes_value - (
        (match_part_number * match_data.part_length) / 60
    )
    if left_over > 0:
        return (
            str(time_in_minutes_value - left_over).split(".")[0]
            + "+"
            + str(left_over).split(".")[0]
        )
    return str(time_in_minutes_value)


def _build_match_events(
    match_data: MatchData,
    *,
    context: MatchTimelineContext,
) -> list[dict[str, Any]]:
    goals = list(
        Shot.objects
        .select_related(
            "player",
            "player__user",
            "shot_type",
            "match_part",
            "team",
        )
        .only(
            "id_uuid",
            "time",
            "for_team",
            "player__id_uuid",
            "player__user__username",
            "shot_type__id_uuid",
            "shot_type__name",
            "match_part__id_uuid",
            "match_part__start_time",
            "match_part__part_number",
            "team__id_uuid",
        )
        .filter(match_data=match_data, scored=True)
        .order_by("time")
        .fetch_mode(models.FETCH_RAISE)
    )

    player_changes = list(
        PlayerChange.objects
        .select_related(
            "player_in",
            "player_in__user",
            "player_out",
            "player_out__user",
            "player_group",
            "player_group__team",
            "match_part",
        )
        .only(
            "id_uuid",
            "time",
            "player_in__id_uuid",
            "player_in__user__username",
            "player_out__id_uuid",
            "player_out__user__username",
            "player_group__id_uuid",
            "player_group__team__id_uuid",
            "match_part__id_uuid",
            "match_part__start_time",
            "match_part__part_number",
        )
        .filter(player_group__match_data=match_data)
        .order_by("time")
        .fetch_mode(models.FETCH_RAISE)
    )

    possession_changes = list(
        PossessionChange.objects
        .select_related("player", "player__user", "team", "match_part")
        .only(
            "id_uuid",
            "kind",
            "time",
            "player__id_uuid",
            "player__user__username",
            "team__id_uuid",
            "match_part__id_uuid",
            "match_part__start_time",
            "match_part__part_number",
        )
        .filter(match_data=match_data)
        .order_by("time")
        .fetch_mode(models.FETCH_RAISE)
    )

    pauses = context.pauses
    sequences = context.event_sequences
    logical_ids = context.logical_event_ids
    timeout_ids_by_pause = {
        pause_id: timeout.timeout_id
        for pause_id, timeout in context.timeouts_by_pause.items()
    }
    events: list[object] = [
        *goals,
        *player_changes,
        *possession_changes,
        *pauses,
    ]
    events.sort(
        key=lambda event: (
            _ordered_sequence(event, sequences, timeout_ids_by_pause) is None,
            _ordered_sequence(event, sequences, timeout_ids_by_pause) or 0,
            _event_time_key(event),
        )
    )

    payload: list[dict[str, Any]] = []

    for event in events:
        serialized = _serialize_match_event(
            match_data,
            event,
            context=context,
        )
        if serialized is not None:
            sequence = _ordered_sequence(event, sequences, timeout_ids_by_pause)
            if sequence is not None:
                serialized["event_sequence"] = sequence
            source_key = _logical_source_key(event, timeout_ids_by_pause)
            if source_key is not None and source_key in logical_ids:
                logical_id = logical_ids[source_key]
                serialized["event_id"] = logical_id
                serialized["logical_event_id"] = logical_id
            payload.append(serialized)

    return payload


def _build_match_shots(
    match_data: MatchData,
    *,
    context: MatchTimelineContext,
) -> list[dict[str, Any]]:
    shots = list(
        Shot.objects
        .select_related(
            "player",
            "player__user",
            "shot_type",
            "match_part",
            "team",
        )
        .only(
            "id_uuid",
            "time",
            "scored",
            "for_team",
            "player__id_uuid",
            "player__user__username",
            "shot_type__id_uuid",
            "shot_type__name",
            "match_part__id_uuid",
            "match_part__start_time",
            "match_part__part_number",
            "team__id_uuid",
        )
        .filter(match_data=match_data)
        .order_by("time")
        .fetch_mode(models.FETCH_RAISE)
    )

    sequences = context.event_sequences
    logical_ids = context.logical_event_ids
    shots.sort(
        key=lambda shot: (
            ("shot", str(shot.id_uuid)) not in sequences,
            sequences.get(("shot", str(shot.id_uuid)), 0),
            _event_time_key(shot),
        )
    )
    payload: list[dict[str, Any]] = []
    for shot in shots:
        serialized = _serialize_shot_timeline_event(
            match_data,
            shot,
            context=context,
        )
        if serialized is not None:
            sequence = sequences.get(("shot", str(shot.id_uuid)))
            if sequence is not None:
                serialized["event_sequence"] = sequence
            logical_id = logical_ids.get(("shot", str(shot.id_uuid)))
            if logical_id is not None:
                serialized["event_id"] = logical_id
                serialized["logical_event_id"] = logical_id
            payload.append(serialized)

    return payload


def build_match_events(
    match_data: MatchData,
    *,
    context: MatchTimelineContext | None = None,
) -> list[dict[str, Any]]:
    """Public wrapper for match event timelines.

    The korfbal-web frontend depends on the exact time formatting produced by
    these payload builders (e.g. "20+1"). Other backends may also reuse the
    same semantics for derived statistics.

    """
    return _build_match_events(
        match_data,
        context=context or load_match_timeline_context(match_data),
    )


def build_match_shots(
    match_data: MatchData,
    *,
    context: MatchTimelineContext | None = None,
) -> list[dict[str, Any]]:
    """Public wrapper for match shot timelines."""
    return _build_match_shots(
        match_data,
        context=context or load_match_timeline_context(match_data),
    )


def build_match_timeline_payloads(
    match_data: MatchData,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Build event and shot projections while sharing their relation context."""
    context = load_match_timeline_context(match_data)
    return (
        _build_match_events(match_data, context=context),
        _build_match_shots(match_data, context=context),
    )


def _serialize_match_event(
    match_data: MatchData,
    event: object,
    *,
    context: MatchTimelineContext | None = None,
) -> dict[str, Any] | None:
    if isinstance(event, Shot):
        return _serialize_goal_event(match_data, event, context=context)
    if isinstance(event, PlayerChange):
        return _serialize_substitute_event(match_data, event, context=context)
    if isinstance(event, PossessionChange):
        return _serialize_possession_change_event(match_data, event, context=context)
    if isinstance(event, Pause):
        return _serialize_pause_event(match_data, event, context=context)
    return None


def _serialize_possession_change_event(
    match_data: MatchData,
    event: PossessionChange,
    *,
    context: MatchTimelineContext | None = None,
) -> dict[str, Any]:
    source_id = str(event.id_uuid)
    return {
        "event_kind": "possession_change",
        "event_id": source_id,
        "logical_event_id": source_id,
        "source_id": source_id,
        "type": "possession_change",
        "name": (
            "Balverlies"
            if event.kind == PossessionChange.BALL_LOSS
            else "Onderschepping"
        ),
        "kind": event.kind,
        "match_part_id": str(event.match_part_id),
        "time_iso": event.time.isoformat(),
        "time": _time_in_minutes(
            match_data=match_data,
            match_part=event.match_part,
            event_time=event.time,
            context=context,
        ),
        "player_id": str(event.player_id) if event.player_id else None,
        "player": event.player.user.username if event.player else None,
        "team_id": str(event.team_id),
        "for_team": event.team_id == match_data.match_link.home_team_id,
    }


def _serialize_goal_event(
    match_data: MatchData,
    event: Shot,
    *,
    context: MatchTimelineContext | None = None,
) -> dict[str, Any] | None:
    if not event.match_part or not event.time or not event.shot_type:
        return None

    if not event.player:
        return None

    # Some trackers fail to set Shot.team for missed shots; in rare cases this
    # also occurs for scored shots. Fall back to group membership.
    team_id = str(event.team.id_uuid) if event.team else None
    if team_id is None and context is not None:
        team_id = context.player_team_ids.get(str(event.player.id_uuid))
    elif team_id is None:
        group = (
            PlayerGroup.objects
            .select_related("team")
            .prefetch_related("players")
            .filter(match_data=match_data, players=event.player)
            .first()
        )
        if group is not None:
            team_id = str(group.team.id_uuid)
    if team_id is None:
        return None
    source_id = str(event.id_uuid)

    return {
        "event_kind": "shot",
        "event_id": source_id,
        "logical_event_id": source_id,
        "source_id": source_id,
        "type": "goal",
        "name": "Gescoord",
        "match_part_id": str(event.match_part.id_uuid),
        "time_iso": event.time.isoformat(),
        "time": _time_in_minutes(
            match_data=match_data,
            match_part=event.match_part,
            event_time=event.time,
            context=context,
        ),
        "player_id": str(event.player.id_uuid),
        "player": event.player.user.username,
        "shot_type_id": str(event.shot_type.id_uuid),
        "goal_type": event.shot_type.name,
        # This flag describes the selected player's role, not the match side:
        # true is the attacker, false is the responsible defender.
        "for_team": bool(event.for_team),
        "team_id": team_id,
    }


def serialize_goal_event(
    match_data: MatchData,
    event: Shot,
) -> dict[str, Any] | None:
    """Serialize a goal event after a write operation."""
    payload = _serialize_goal_event(match_data, event)
    if payload is not None:
        logical_id = event_root_ids(match_data).get(("shot", str(event.id_uuid)))
        if logical_id is not None:
            payload["event_id"] = logical_id
            payload["logical_event_id"] = logical_id
    return payload


def _serialize_shot_timeline_event(
    match_data: MatchData,
    event: Shot,
    *,
    context: MatchTimelineContext | None = None,
) -> dict[str, Any] | None:
    if not event.player:
        return None

    # Shots can be recorded while the tracker is still syncing match parts/timers.
    # We still want to return them for the advanced timeline, even when part/time
    # metadata is missing.
    team_id = str(event.team.id_uuid) if event.team else None
    if team_id is None and context is not None:
        team_id = context.player_team_ids.get(str(event.player.id_uuid))
    if team_id is None:
        return None

    payload: dict[str, Any] = {
        "event_id": str(event.id_uuid),
        "source_id": str(event.id_uuid),
        "time": "?",
        "player_id": str(event.player.id_uuid),
        "player": event.player.user.username,
        "shot_type_id": str(event.shot_type.id_uuid) if event.shot_type else None,
        "shot_type": event.shot_type.name if event.shot_type else None,
        "scored": bool(event.scored),
        "for_team": bool(event.for_team),
        "team_id": team_id,
    }

    if event.time is not None:
        payload["time_iso"] = event.time.isoformat()

    if event.match_part is not None:
        payload["match_part_id"] = str(event.match_part.id_uuid)

    if event.match_part is not None and event.time is not None:
        payload["time"] = _time_in_minutes(
            match_data=match_data,
            match_part=event.match_part,
            event_time=event.time,
            context=context,
        )

    return payload


def _serialize_substitute_event(
    match_data: MatchData,
    event: PlayerChange,
    *,
    context: MatchTimelineContext | None = None,
) -> dict[str, Any] | None:
    if not event.time:
        return None

    has_players = bool(event.player_in) and bool(event.player_out)
    name = "Wissel" if has_players else "Wissel tegenstander"

    source_id = str(event.id_uuid)
    payload: dict[str, Any] = {
        "event_kind": "player_change",
        "event_id": source_id,
        "logical_event_id": source_id,
        "source_id": source_id,
        "type": "substitute",
        "name": name,
        "time_iso": event.time.isoformat(),
        "player_in_id": str(event.player_in.id_uuid) if event.player_in else None,
        "player_in": event.player_in.user.username if event.player_in else None,
        "player_out_id": str(event.player_out.id_uuid) if event.player_out else None,
        "player_out": event.player_out.user.username if event.player_out else None,
        "player_group_id": str(event.player_group.id_uuid),
        "team_id": str(event.player_group.team.id_uuid),
    }

    if event.match_part:
        payload["match_part_id"] = str(event.match_part.id_uuid)
        payload["time"] = _time_in_minutes(
            match_data=match_data,
            match_part=event.match_part,
            event_time=event.time,
            context=context,
        )
    else:
        payload["time"] = _intermission_label_for_time(
            match_data,
            event.time,
            context=context,
        )

    return payload


def serialize_substitute_event(
    match_data: MatchData,
    event: PlayerChange,
) -> dict[str, Any] | None:
    """Serialize a substitution event after a write operation."""
    payload = _serialize_substitute_event(match_data, event)
    if payload is not None:
        logical_id = event_root_ids(match_data).get((
            "player_change",
            str(event.id_uuid),
        ))
        if logical_id is not None:
            payload["event_id"] = logical_id
            payload["logical_event_id"] = logical_id
    return payload


def _serialize_pause_event(
    match_data: MatchData,
    event: Pause,
    *,
    context: MatchTimelineContext | None = None,
) -> dict[str, Any] | None:
    if not event.match_part or not event.start_time:
        return None

    timeout_data = (
        context.timeouts_by_pause.get(str(event.id_uuid))
        if context is not None
        else None
    )
    timeout = (
        None
        if context is not None
        else Timeout.objects.select_related("team").filter(pause=event).first()
    )
    timeout_id = timeout_data.timeout_id if timeout_data else None
    timeout_team_id = timeout_data.team_id if timeout_data else None
    if timeout is not None:
        timeout_id = str(timeout.id_uuid)
        timeout_team_id = str(timeout.team_id) if timeout.team_id else None
    source_id = str(event.id_uuid)

    return {
        "event_kind": "timeout" if timeout_id else "pause",
        # The wrapper replaces this projection id with the canonical timeout root.
        "event_id": source_id,
        "logical_event_id": source_id,
        "source_id": source_id,
        "pause_id": str(event.id_uuid),
        "timeout_id": timeout_id,
        "type": "intermission",
        "name": "Time-out" if timeout_id else "Pauze",
        "match_part_id": str(event.match_part.id_uuid),
        "team_id": timeout_team_id,
        "time": _time_in_minutes(
            match_data=match_data,
            match_part=event.match_part,
            event_time=event.start_time,
            context=context,
        ),
        "length": event.length().total_seconds(),
        "start_time": (event.start_time.isoformat() if event.start_time else None),
        "end_time": event.end_time.isoformat() if event.end_time else None,
    }


def serialize_pause_event(
    match_data: MatchData,
    event: Pause,
) -> dict[str, Any] | None:
    """Serialize a pause/timeout event after a write operation."""
    payload = _serialize_pause_event(match_data, event)
    if payload is not None:
        root_id = logical_event_id(
            match_data,
            source_type="pause",
            source_id=event.id_uuid,
        )
        payload["event_id"] = root_id
        payload["logical_event_id"] = root_id
    return payload
