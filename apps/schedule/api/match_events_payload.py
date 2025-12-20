"""Payload builders for match event timelines.

These are *not* DRF serializers; they are lightweight dict payload builders used
by korfbal-web for match event timelines and graphs.

Keeping these helpers out of `views.py` significantly reduces file size and
improves testability.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from apps.game_tracker.models import (
    MatchData,
    MatchPart,
    Pause,
    PlayerChange,
    Shot,
    Timeout,
)


PART_ONE = 1
PART_TWO = 2


def _intermission_label_for_time(match_data: MatchData, event_time: datetime) -> str:
    """Return a human label for events that happened between match parts.

    We intentionally keep this as a string label (instead of forcing an artificial
    part-relative minute) so the frontend doesn't show it as added time ("30+X")
    for the previous part.

    """
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


def _time_in_minutes(
    *,
    match_data: MatchData,
    match_part_start: datetime,
    match_part_number: int,
    event_time: datetime,
) -> str:
    pauses = Pause.objects.filter(
        match_data=match_data,
        active=False,
        start_time__lt=event_time,
        start_time__gte=match_part_start,
    )
    pause_time = sum(pause.length().total_seconds() for pause in pauses)

    time_in_minutes_value = round(
        (
            (event_time - match_part_start).total_seconds()
            + ((match_part_number - 1) * int(match_data.part_length))
            - pause_time
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


def _build_match_events(match_data: MatchData) -> list[dict[str, Any]]:
    goals = list(
        Shot.objects
        .select_related(
            "player__user",
            "shot_type",
            "match_part",
            "team",
        )
        .filter(match_data=match_data, scored=True)
        .order_by("time")
    )

    player_changes = list(
        PlayerChange.objects
        .select_related(
            "player_in__user",
            "player_out__user",
            "player_group",
            "match_part",
        )
        .filter(player_group__match_data=match_data)
        .order_by("time")
    )

    pauses = list(
        Pause.objects
        .select_related("match_part")
        .filter(match_data=match_data)
        .order_by("start_time")
    )

    events: list[object] = [*goals, *player_changes, *pauses]
    events.sort(key=_event_time_key)

    payload: list[dict[str, Any]] = []

    for event in events:
        serialized = _serialize_match_event(match_data, event)
        if serialized is not None:
            payload.append(serialized)

    return payload


def _build_match_shots(match_data: MatchData) -> list[dict[str, Any]]:
    shots = list(
        Shot.objects
        .select_related(
            "player__user",
            "shot_type",
            "match_part",
            "team",
        )
        .filter(match_data=match_data)
        .order_by("time")
    )

    payload: list[dict[str, Any]] = []
    for shot in shots:
        serialized = _serialize_shot_timeline_event(match_data, shot)
        if serialized is not None:
            payload.append(serialized)

    return payload


def build_match_events(match_data: MatchData) -> list[dict[str, Any]]:
    """Public wrapper for match event timelines.

    The korfbal-web frontend depends on the exact time formatting produced by
    these payload builders (e.g. "20+1"). Other backends may also reuse the
    same semantics for derived statistics.

    """
    return _build_match_events(match_data)


def build_match_shots(match_data: MatchData) -> list[dict[str, Any]]:
    """Public wrapper for match shot timelines."""
    return _build_match_shots(match_data)


def _serialize_match_event(
    match_data: MatchData,
    event: object,
) -> dict[str, Any] | None:
    if isinstance(event, Shot):
        return _serialize_goal_event(match_data, event)
    if isinstance(event, PlayerChange):
        return _serialize_substitute_event(match_data, event)
    if isinstance(event, Pause):
        return _serialize_pause_event(match_data, event)
    return None


def _serialize_goal_event(match_data: MatchData, event: Shot) -> dict[str, Any] | None:
    if not event.match_part or not event.time or not event.team or not event.shot_type:
        return None

    return {
        "event_kind": "shot",
        "event_id": str(event.id_uuid),
        "type": "goal",
        "name": "Gescoord",
        "match_part_id": str(event.match_part.id_uuid),
        "time_iso": event.time.isoformat(),
        "time": _time_in_minutes(
            match_data=match_data,
            match_part_start=event.match_part.start_time,
            match_part_number=event.match_part.part_number,
            event_time=event.time,
        ),
        "player_id": str(event.player.id_uuid),
        "player": event.player.user.username,
        "shot_type_id": str(event.shot_type.id_uuid),
        "goal_type": event.shot_type.name,
        "for_team": event.for_team,
        "team_id": str(event.team.id_uuid),
    }


def _serialize_shot_timeline_event(
    match_data: MatchData,
    event: Shot,
) -> dict[str, Any] | None:
    if not event.match_part or not event.time or not event.team:
        return None

    return {
        "event_id": str(event.id_uuid),
        "match_part_id": str(event.match_part.id_uuid),
        "time_iso": event.time.isoformat(),
        "time": _time_in_minutes(
            match_data=match_data,
            match_part_start=event.match_part.start_time,
            match_part_number=event.match_part.part_number,
            event_time=event.time,
        ),
        "player_id": str(event.player.id_uuid),
        "player": event.player.user.username,
        "shot_type_id": str(event.shot_type.id_uuid) if event.shot_type else None,
        "shot_type": event.shot_type.name if event.shot_type else None,
        "scored": bool(event.scored),
        "team_id": str(event.team.id_uuid),
    }


def _serialize_substitute_event(
    match_data: MatchData,
    event: PlayerChange,
) -> dict[str, Any] | None:
    if not event.time:
        return None

    has_players = bool(event.player_in) and bool(event.player_out)
    name = "Wissel" if has_players else "Wissel tegenstander"

    payload: dict[str, Any] = {
        "event_kind": "player_change",
        "event_id": str(event.id_uuid),
        "type": "substitute",
        "name": name,
        "time_iso": event.time.isoformat(),
        "player_in_id": str(event.player_in.id_uuid) if event.player_in else None,
        "player_in": event.player_in.user.username if event.player_in else None,
        "player_out_id": str(event.player_out.id_uuid) if event.player_out else None,
        "player_out": event.player_out.user.username if event.player_out else None,
        "player_group_id": str(event.player_group.id_uuid),
    }

    if event.match_part:
        payload["match_part_id"] = str(event.match_part.id_uuid)
        payload["time"] = _time_in_minutes(
            match_data=match_data,
            match_part_start=event.match_part.start_time,
            match_part_number=event.match_part.part_number,
            event_time=event.time,
        )
    else:
        payload["time"] = _intermission_label_for_time(match_data, event.time)

    return payload


def _serialize_pause_event(
    match_data: MatchData,
    event: Pause,
) -> dict[str, Any] | None:
    if not event.match_part or not event.start_time:
        return None

    timeout = Timeout.objects.select_related("team").filter(pause=event).first()

    return {
        "event_kind": "timeout" if timeout else "pause",
        "event_id": str(timeout.id_uuid) if timeout else str(event.id_uuid),
        "pause_id": str(event.id_uuid),
        "type": "intermission",
        "name": "Time-out" if timeout else "Pauze",
        "match_part_id": str(event.match_part.id_uuid),
        "team_id": str(timeout.team_id) if timeout else None,
        "time": _time_in_minutes(
            match_data=match_data,
            match_part_start=event.match_part.start_time,
            match_part_number=event.match_part.part_number,
            event_time=event.start_time,
        ),
        "length": event.length().total_seconds(),
        "start_time": (event.start_time.isoformat() if event.start_time else None),
        "end_time": event.end_time.isoformat() if event.end_time else None,
    }
