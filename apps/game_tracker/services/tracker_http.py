"""HTTP-friendly match tracker helpers.

The legacy match tracker UI used a WebSocket consumer (`MatchTrackerConsumer`).
The React rewrite uses plain HTTP:

- client sends commands via POST
- clients can long-poll for updates

This module implements the tracker state computation and command side-effects in
sync Django ORM code so it can be reused from DRF views.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
import time
from typing import Any, cast
from uuid import UUID

from django.db import transaction
from django.utils import timezone

from apps.game_tracker.models import (
    Attack,
    GoalType,
    GroupType,
    MatchData,
    MatchPart,
    Pause,
    PlayerChange,
    PlayerGroup,
    Shot,
    Timeout,
)
from apps.game_tracker.services.match_scores import compute_scores_for_matchdata_ids
from apps.player.models import Player
from apps.schedule.models import Match
from apps.team.models.team import Team


MATCH_TRACKER_DATA_NOT_FOUND = "Match tracker data not found."
MATCH_IS_PAUSED_MESSAGE = "match is paused"
NO_ACTIVE_MATCH_PART_MESSAGE = "No active match part."


class TrackerCommandError(RuntimeError):
    """Raised when a tracker command cannot be applied."""

    def __init__(self, message: str, *, code: str = "error") -> None:
        """Create an error that can be mapped to an API response."""
        super().__init__(message)
        self.code = code


def _other_team(match: Match, team: Team) -> Team:
    home_team_id = cast(Any, match).home_team_id
    return match.away_team if home_team_id == team.id_uuid else match.home_team


def _current_part(match_data: MatchData) -> MatchPart | None:
    return MatchPart.objects.filter(match_data=match_data, active=True).first()


def _is_paused(match_data: MatchData, current_part: MatchPart | None) -> bool:
    if match_data.status != "active":
        return True
    if not current_part:
        # Match marked active but no active part => treat as paused like the WS
        # consumer does (button shows Start).
        return True
    return Pause.objects.filter(
        match_data=match_data,
        active=True,
        match_part=current_part,
    ).exists()


def _timer_data(
    match_data: MatchData,
    current_part: MatchPart | None,
) -> dict[str, Any]:
    if not current_part:
        return {
            "type": "deactivated",
            "match_data_id": str(match_data.id_uuid),
        }

    active_pause = Pause.objects.filter(
        match_data=match_data,
        active=True,
        match_part=current_part,
    ).first()

    pauses = Pause.objects.filter(
        match_data=match_data,
        active=False,
        match_part=current_part,
    )
    pause_time = sum(pause.length().total_seconds() for pause in pauses)

    base: dict[str, Any] = {
        "match_data_id": str(match_data.id_uuid),
        "time": current_part.start_time.isoformat(),
        "length": match_data.part_length,
        "pause_length": pause_time,
        "server_time": datetime.now(UTC).isoformat(),
    }

    if active_pause and active_pause.start_time:
        return {
            **base,
            "type": "pause",
            "calc_to": active_pause.start_time.isoformat(),
        }

    return {
        **base,
        "type": "active",
    }


def _score(match_data: MatchData, *, team: Team, opponent: Team) -> tuple[int, int]:
    goals_for = Shot.objects.filter(
        match_data=match_data,
        team=team,
        scored=True,
    ).count()
    goals_against = Shot.objects.filter(
        match_data=match_data,
        team=opponent,
        scored=True,
    ).count()
    return goals_for, goals_against


def _swap_player_group_types(match_data: MatchData, team: Team) -> None:
    group_type_attack = GroupType.objects.get(name="Aanval")
    group_type_defense = GroupType.objects.get(name="Verdediging")

    pg_attack = PlayerGroup.objects.get(
        match_data=match_data,
        team=team,
        current_type=group_type_attack,
    )
    pg_defense = PlayerGroup.objects.get(
        match_data=match_data,
        team=team,
        current_type=group_type_defense,
    )

    pg_attack.current_type = group_type_defense
    pg_defense.current_type = group_type_attack
    pg_attack.save(update_fields=["current_type"])
    pg_defense.save(update_fields=["current_type"])


def _player_groups_payload(
    match_data: MatchData,
    *,
    team: Team,
    opponent: Team,
) -> list[dict[str, Any]]:
    player_groups = (
        PlayerGroup.objects.select_related("starting_type", "current_type")
        .prefetch_related("players__user")
        .filter(match_data=match_data, team=team)
        .exclude(starting_type__name="Reserve")
        .order_by(
            # Put the currently attacking group first.
            # (Same idea as the WS consumer ordering.)
            "current_type__name",
            "starting_type__name",
        )
    )

    # We want Aanval first, then Verdediging. Ordering by name isn't stable in
    # all locales, so we reorder in Python.
    ordered: list[PlayerGroup] = []
    aanval = [pg for pg in player_groups if pg.current_type.name == "Aanval"]
    verdediging = [pg for pg in player_groups if pg.current_type.name == "Verdediging"]
    ordered.extend(aanval)
    ordered.extend(verdediging)

    result: list[dict[str, Any]] = []
    for pg in ordered:
        players_payload: list[dict[str, Any]] = []
        for p in pg.players.all():
            shots_for = Shot.objects.filter(
                match_data=match_data,
                player=p,
                team=team,
            ).count()
            shots_against = Shot.objects.filter(
                match_data=match_data,
                player=p,
                team=opponent,
            ).count()
            goals_for = Shot.objects.filter(
                match_data=match_data,
                player=p,
                team=team,
                scored=True,
            ).count()
            goals_against = Shot.objects.filter(
                match_data=match_data,
                player=p,
                team=opponent,
                scored=True,
            ).count()

            players_payload.append({
                "id": str(p.id_uuid),
                "name": p.user.username,
                "shots_for": shots_for,
                "shots_against": shots_against,
                "goals_for": goals_for,
                "goals_against": goals_against,
            })

        result.append({
            "id": str(pg.id_uuid),
            "starting_type": pg.starting_type.name,
            "current_type": pg.current_type.name,
            "players": players_payload,
        })
    return result


def _reserve_players_payload(
    match_data: MatchData,
    *,
    team: Team,
) -> list[dict[str, Any]]:
    reserve_group = (
        PlayerGroup.objects.prefetch_related("players__user")
        .filter(
            match_data=match_data,
            team=team,
            starting_type__name="Reserve",
        )
        .first()
    )
    if not reserve_group:
        return []
    return [
        {"id": str(p.id_uuid), "name": p.user.username}
        for p in reserve_group.players.all()
    ]


def _last_event_key(event: object) -> datetime:
    value = getattr(event, "time", None)
    if isinstance(value, datetime):
        return value
    value = getattr(event, "start_time", None)
    if isinstance(value, datetime):
        return value
    return datetime.min.replace(tzinfo=UTC)


def _get_last_event_model(match_data: MatchData) -> object | None:
    candidates: list[object] = []

    shot = (
        Shot.objects.select_related("player__user", "shot_type", "match_part", "team")
        .filter(match_data=match_data)
        .order_by("-time")
        .first()
    )
    if shot and shot.time:
        candidates.append(shot)

    change = (
        PlayerChange.objects.select_related(
            "player_in__user",
            "player_out__user",
            "player_group",
            "match_part",
        )
        .filter(match_data=match_data)
        .order_by("-time")
        .first()
    )
    if change and change.time:
        candidates.append(change)

    pause = (
        Pause.objects.select_related("match_part")
        .filter(match_data=match_data)
        .order_by("-start_time")
        .first()
    )
    if pause and pause.start_time:
        candidates.append(pause)

    attack = (
        Attack.objects.select_related("team")
        .filter(match_data=match_data)
        .order_by("-time")
        .first()
    )
    if attack and attack.time:
        candidates.append(attack)

    if not candidates:
        return None
    candidates.sort(key=_last_event_key)
    return candidates[-1]


def _last_event_payload(
    match_data: MatchData,
    *,
    team: Team,
    opponent: Team,
) -> dict[str, Any]:
    event = _get_last_event_model(match_data)
    if not event:
        return {"type": "no_event"}

    goals_for, goals_against = _score(match_data, team=team, opponent=opponent)

    if isinstance(event, Shot):
        return _serialize_last_event_shot(
            event,
            team=team,
            goals_for=goals_for,
            goals_against=goals_against,
        )
    if isinstance(event, PlayerChange):
        return _serialize_last_event_player_change(event)
    if isinstance(event, Pause):
        return _serialize_last_event_pause(event)
    if isinstance(event, Attack):
        return _serialize_last_event_attack(event)
    return {"type": "no_event"}


def _serialize_last_event_shot(
    event: Shot,
    *,
    team: Team,
    goals_for: int,
    goals_against: int,
) -> dict[str, Any]:
    if not event.time:
        return {"type": "no_event"}

    team_id = cast(Any, event).team_id
    common: dict[str, Any] = {
        "id": str(event.id_uuid),
        "player": event.player.user.username,
        "player_id": str(event.player.id_uuid),
        "for_team": bool(team_id == team.id_uuid),
        "team_id": str(team_id) if team_id else None,
        "time_iso": event.time.isoformat(),
        "time": event.time.isoformat(),
    }

    if event.scored and event.shot_type:
        return {
            **common,
            "type": "goal",
            "name": "Gescoord",
            "shot_type": event.shot_type.name,
            "shot_type_id": str(event.shot_type.id_uuid),
            "goals_for": goals_for,
            "goals_against": goals_against,
        }

    return {
        **common,
        "type": "shot",
        "name": "Schot",
    }


def _serialize_last_event_player_change(event: PlayerChange) -> dict[str, Any]:
    if not event.time:
        return {"type": "no_event"}
    if not event.player_in or not event.player_out:
        return {
            "type": "substitute",
            "id": str(event.id_uuid),
            "name": "Wissel tegenstander",
            "player_in": None,
            "player_in_id": None,
            "player_out": None,
            "player_out_id": None,
            "player_group_id": str(event.player_group.id_uuid),
            "time_iso": event.time.isoformat(),
            "time": event.time.isoformat(),
        }
    return {
        "type": "substitute",
        "id": str(event.id_uuid),
        "name": "Wissel",
        "player_in": event.player_in.user.username,
        "player_in_id": str(event.player_in.id_uuid),
        "player_out": event.player_out.user.username,
        "player_out_id": str(event.player_out.id_uuid),
        "player_group_id": str(event.player_group.id_uuid),
        "time_iso": event.time.isoformat(),
        "time": event.time.isoformat(),
    }


def _serialize_last_event_pause(event: Pause) -> dict[str, Any]:
    if not event.start_time:
        return {"type": "no_event"}
    timeout = Timeout.objects.select_related("team").filter(pause=event).first()
    return {
        "type": "pause",
        "id": str(event.id_uuid),
        "pause_id": str(event.id_uuid),
        "name": "Time-out" if timeout else "Pauze",
        "event_kind": "timeout" if timeout else "pause",
        "team_id": str(timeout.team_id) if timeout and timeout.team_id else None,
        "start_time": event.start_time.isoformat(),
        "end_time": event.end_time.isoformat() if event.end_time else None,
        "active": event.active,
    }


def _serialize_last_event_attack(event: Attack) -> dict[str, Any]:
    if not event.time:
        return {"type": "no_event"}

    team_id = cast(Any, event).team_id
    return {
        "type": "attack",
        "id": str(event.id_uuid),
        "name": "Aanval",
        "team": event.team.name if event.team else None,
        "team_id": str(team_id) if team_id else None,
        "time_iso": event.time.isoformat(),
        "time": event.time.isoformat(),
    }


def _last_changed_at(match_data: MatchData) -> datetime:
    """Best-effort change marker for long-polling.

    We don't have updated_at fields on the models, so we approximate with relevant
    timestamps that *do* change on update (pause end_time, match_part end_time, etc.).
    """
    candidates: list[datetime] = []

    shot_time = (
        Shot.objects.filter(match_data=match_data)
        .order_by("-time")
        .values_list("time", flat=True)
        .first()
    )
    if isinstance(shot_time, datetime):
        candidates.append(shot_time)

    change_time = (
        PlayerChange.objects.filter(match_data=match_data)
        .order_by("-time")
        .values_list("time", flat=True)
        .first()
    )
    if isinstance(change_time, datetime):
        candidates.append(change_time)

    pause_change = (
        Pause.objects.filter(match_data=match_data)
        .order_by("-start_time")
        .values_list("start_time", flat=True)
        .first()
    )
    if isinstance(pause_change, datetime):
        candidates.append(pause_change)

    pause_end = (
        Pause.objects.filter(match_data=match_data, end_time__isnull=False)
        .order_by("-end_time")
        .values_list("end_time", flat=True)
        .first()
    )
    if isinstance(pause_end, datetime):
        candidates.append(pause_end)

    part_start = (
        MatchPart.objects.filter(match_data=match_data)
        .order_by("-start_time")
        .values_list("start_time", flat=True)
        .first()
    )
    if isinstance(part_start, datetime):
        candidates.append(part_start)

    part_end = (
        MatchPart.objects.filter(match_data=match_data, end_time__isnull=False)
        .order_by("-end_time")
        .values_list("end_time", flat=True)
        .first()
    )
    if isinstance(part_end, datetime):
        candidates.append(part_end)

    attack_time = (
        Attack.objects.filter(match_data=match_data)
        .order_by("-time")
        .values_list("time", flat=True)
        .first()
    )
    if isinstance(attack_time, datetime):
        candidates.append(attack_time)

    if not candidates:
        return datetime.min.replace(tzinfo=UTC)
    return max(candidates)


def get_tracker_state(match: Match, *, team: Team) -> dict[str, Any]:
    """Return a snapshot of the current tracker state.

    Raises:
        TrackerCommandError: If the tracker data for the match does not exist.

    """
    match_data = MatchData.objects.filter(match_link=match).first()
    if not match_data:
        raise TrackerCommandError(MATCH_TRACKER_DATA_NOT_FOUND, code="not_found")

    opponent = _other_team(match, team)
    current_part = _current_part(match_data)

    goals_for, goals_against = _score(match_data, team=team, opponent=opponent)
    paused = _is_paused(match_data, current_part)

    start_stop_label = "Start"
    if match_data.status == "active" and not paused:
        start_stop_label = "Pauze"

    goal_types = list(GoalType.objects.order_by("name"))

    substitutions_max = 8
    substitutions_for = PlayerChange.objects.filter(
        match_data=match_data,
        player_group__team=team,
    ).count()
    substitutions_against = PlayerChange.objects.filter(
        match_data=match_data,
        player_group__team=opponent,
    ).count()
    substitutions_total = substitutions_for + substitutions_against

    timeouts_max = 2
    timeouts_for = Timeout.objects.filter(match_data=match_data, team=team).count()
    timeouts_against = Timeout.objects.filter(
        match_data=match_data,
        team=opponent,
    ).count()

    state: dict[str, Any] = {
        "match_id": str(match.id_uuid),
        "match_data_id": str(match_data.id_uuid),
        "status": match_data.status,
        "parts": match_data.parts,
        "current_part": match_data.current_part,
        "part_length": match_data.part_length,
        "team": {
            "id": str(team.id_uuid),
            "name": team.name,
            "club": team.club.name,
        },
        "opponent": {
            "id": str(opponent.id_uuid),
            "name": opponent.name,
            "club": opponent.club.name,
        },
        "score": {
            "for": goals_for,
            "against": goals_against,
        },
        "substitutions": {
            "for": substitutions_for,
            "against": substitutions_against,
            "max": substitutions_max,
        },
        "timeouts": {
            "for": timeouts_for,
            "against": timeouts_against,
            "max": timeouts_max,
        },
        "substitutions_total": substitutions_total,
        "paused": paused,
        "start_stop_label": start_stop_label,
        "timer": _timer_data(match_data, current_part),
        "player_groups": _player_groups_payload(
            match_data,
            team=team,
            opponent=opponent,
        ),
        "reserve_players": _reserve_players_payload(match_data, team=team),
        "goal_types": [{"id": str(gt.id_uuid), "name": gt.name} for gt in goal_types],
        "last_event": _last_event_payload(match_data, team=team, opponent=opponent),
        "last_changed_at": _last_changed_at(match_data).isoformat(),
    }

    return state


def _require_not_paused(
    match_data: MatchData,
    team: Team,
    match: Match,
) -> tuple[MatchPart, Team]:
    current_part = _current_part(match_data)
    opponent = _other_team(match, team)
    if _is_paused(match_data, current_part):
        raise TrackerCommandError(MATCH_IS_PAUSED_MESSAGE, code="match_paused")
    if not current_part:
        raise TrackerCommandError(
            NO_ACTIVE_MATCH_PART_MESSAGE,
            code="no_active_part",
        )
    return current_part, opponent


def apply_tracker_command(
    match: Match,
    *,
    team: Team,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Apply a tracker command and return the updated state.

    Raises:
        TrackerCommandError: If the command is invalid or cannot be applied.

    """
    command = payload.get("command")
    if not isinstance(command, str):
        raise TrackerCommandError("Missing command.", code="bad_request")

    match_data = MatchData.objects.filter(match_link=match).first()
    if not match_data:
        raise TrackerCommandError(MATCH_TRACKER_DATA_NOT_FOUND, code="not_found")

    with transaction.atomic():
        # Refresh for consistent reads inside the transaction.
        match_data = MatchData.objects.select_for_update().get(
            id_uuid=match_data.id_uuid,
        )

        _dispatch_command(match, match_data=match_data, team=team, payload=payload)

    return get_tracker_state(match, team=team)


def poll_tracker_state(
    match: Match,
    *,
    team: Team,
    since: datetime,
    timeout_seconds: int = 25,
) -> dict[str, Any]:
    """Long-poll until tracker state changed or timeout.

    Notes:
        - This is implemented as a simple sleep loop (thread blocking). Keep
          `timeout_seconds` modest.

    Raises:
        TrackerCommandError: If the tracker data for the match does not exist.

    """
    timeout_seconds = max(1, min(timeout_seconds, 30))
    deadline = time.monotonic() + timeout_seconds

    match_data = MatchData.objects.filter(match_link=match).first()
    if not match_data:
        raise TrackerCommandError(MATCH_TRACKER_DATA_NOT_FOUND, code="not_found")

    while True:
        changed_at = _last_changed_at(match_data)
        if changed_at > since:
            return get_tracker_state(match, team=team)

        if time.monotonic() >= deadline:
            # Return a lightweight response on timeout so the client can poll again.
            return {
                "changed": False,
                "server_time": timezone.now().isoformat(),
                "last_changed_at": changed_at.isoformat(),
            }

        time.sleep(0.8)
        match_data = MatchData.objects.filter(match_link=match).first()
        if not match_data:
            raise TrackerCommandError(MATCH_TRACKER_DATA_NOT_FOUND, code="not_found")


def _cmd_start_pause(*, match_data: MatchData) -> None:
    current_part = _current_part(match_data)
    if not current_part:
        MatchPart.objects.create(
            match_data=match_data,
            active=True,
            start_time=datetime.now(UTC),
            part_number=match_data.current_part,
        )

        if match_data.current_part == 1:
            match_data.status = "active"
            match_data.save(update_fields=["status"])

        return

    # Toggle pause.
    active_pause = Pause.objects.filter(
        match_data=match_data,
        active=True,
        match_part=current_part,
    ).first()

    if not active_pause:
        Pause.objects.create(
            match_data=match_data,
            active=True,
            start_time=datetime.now(UTC),
            match_part=current_part,
        )
        return

    active_pause.active = False
    active_pause.end_time = datetime.now(UTC)
    active_pause.save(update_fields=["active", "end_time"])


def _cmd_part_end(_match: Match, *, match_data: MatchData) -> None:
    del _match
    # Defensive cleanup: if a pause is active while a part ends, always close it.
    # In some edge cases the active MatchPart may already be deactivated or
    # temporarily missing, but an active Pause would still break timer logic.
    now = datetime.now(UTC)
    active_pauses = Pause.objects.filter(match_data=match_data, active=True)
    if active_pauses.exists():
        active_pauses.update(active=False, end_time=now)

    current_part = _current_part(match_data)
    # Note: pause cleanup is handled above (not scoped to current_part) to be
    # resilient to inconsistent state.

    if match_data.current_part < match_data.parts:
        match_data.current_part += 1
        match_data.save(update_fields=["current_part"])

        if current_part:
            current_part.active = False
            current_part.end_time = datetime.now(UTC)
            current_part.save(update_fields=["active", "end_time"])
        return

    # End match.
    match_data_uuid = match_data.id_uuid
    match_data_id = (
        match_data_uuid
        if isinstance(match_data_uuid, UUID)
        else UUID(str(match_data_uuid))
    )
    scores = compute_scores_for_matchdata_ids([match_data_id]).get(
        match_data_id, (0, 0)
    )

    match_data.status = "finished"
    match_data.home_score, match_data.away_score = scores
    match_data.save(update_fields=["status", "home_score", "away_score"])
    if current_part:
        current_part.active = False
        current_part.end_time = datetime.now(UTC)
        current_part.save(update_fields=["active", "end_time"])


def _cmd_timeout(match: Match, *, match_data: MatchData, team: Team) -> None:
    current_part, _ = _require_not_paused(match_data, team, match)

    # A timeout is essentially: pause + timeout record.
    Pause.objects.create(
        match_data=match_data,
        active=True,
        start_time=datetime.now(UTC),
        match_part=current_part,
    )
    pause = (
        Pause.objects.filter(
            match_data=match_data,
            match_part=current_part,
            active=True,
        )
        .order_by("-start_time")
        .first()
    )
    if not pause:
        raise TrackerCommandError(
            "Failed to create pause for timeout.",
            code="server_error",
        )
    Timeout.objects.create(
        match_data=match_data,
        match_part=current_part,
        team=team,
        pause=pause,
    )


def _cmd_new_attack(match: Match, *, match_data: MatchData, team: Team) -> None:
    current_part, _ = _require_not_paused(match_data, team, match)
    Attack.objects.create(
        match_data=match_data,
        match_part=current_part,
        team=team,
        time=datetime.now(UTC),
    )


@dataclass(frozen=True, slots=True)
class _ShotRegParams:
    player_id: str
    for_team: bool
    shot_type_id: str | None = None


def _cmd_shot_reg(
    match: Match,
    *,
    match_data: MatchData,
    team: Team,
    params: _ShotRegParams,
) -> None:
    current_part, opponent = _require_not_paused(match_data, team, match)

    player = Player.objects.get(id_uuid=params.player_id)
    shot_team = team if params.for_team else opponent

    shot_type: GoalType | None = None
    if params.shot_type_id:
        try:
            shot_type = GoalType.objects.get(id_uuid=params.shot_type_id)
        except GoalType.DoesNotExist as exc:
            raise TrackerCommandError(
                "Invalid shot type.",
                code="bad_request",
            ) from exc

    Shot.objects.create(
        player=player,
        match_data=match_data,
        match_part=current_part,
        time=datetime.now(UTC),
        for_team=params.for_team,
        team=shot_team,
        shot_type=shot_type,
        scored=False,
    )


@dataclass(frozen=True, slots=True)
class _GoalRegParams:
    player_id: str
    goal_type_id: str
    for_team: bool


def _cmd_goal_reg(
    match: Match,
    *,
    match_data: MatchData,
    team: Team,
    params: _GoalRegParams,
) -> None:
    current_part, opponent = _require_not_paused(match_data, team, match)

    player = Player.objects.select_related("user").get(id_uuid=params.player_id)
    goal_type = GoalType.objects.get(id_uuid=params.goal_type_id)
    shot_team = team if params.for_team else opponent

    Shot.objects.create(
        player=player,
        match_data=match_data,
        match_part=current_part,
        time=datetime.now(UTC),
        for_team=params.for_team,
        team=shot_team,
        shot_type=goal_type,
        scored=True,
    )

    number_of_goals = Shot.objects.filter(
        match_data=match_data,
        scored=True,
    ).count()
    if number_of_goals % 2 == 0:
        _swap_player_group_types(match_data, team)
        _swap_player_group_types(match_data, opponent)


def _cmd_substitute_reg(
    match: Match,
    *,
    match_data: MatchData,
    team: Team,
    new_player_id: str,
    old_player_id: str,
) -> None:
    del match
    current_part = _current_part(match_data)
    if current_part and _is_paused(match_data, current_part):
        raise TrackerCommandError(MATCH_IS_PAUSED_MESSAGE, code="match_paused")

    # Allow substitutions when there is no active part (between parts). In that
    # situation, attach the substitution to the most recently completed part.
    part_for_event = current_part
    if not part_for_event:
        if match_data.status != "active":
            raise TrackerCommandError("Match is not active.", code="match_not_active")
        if match_data.current_part <= 1:
            raise TrackerCommandError(
                NO_ACTIVE_MATCH_PART_MESSAGE,
                code="no_active_part",
            )

        previous_part_number = match_data.current_part - 1
        part_for_event = (
            MatchPart.objects.filter(
                match_data=match_data,
                part_number=previous_part_number,
            )
            .order_by("-start_time")
            .first()
        )
        if not part_for_event:
            # Fallback: take the latest part in case numbering is inconsistent.
            part_for_event = (
                MatchPart.objects.filter(match_data=match_data)
                .order_by("-part_number", "-start_time")
                .first()
            )
        if not part_for_event:
            raise TrackerCommandError(
                NO_ACTIVE_MATCH_PART_MESSAGE,
                code="no_active_part",
            )

    substitutions_max = 8
    substitutions_for = PlayerChange.objects.filter(
        match_data=match_data,
        player_group__team=team,
    ).count()
    if substitutions_for >= substitutions_max:
        raise TrackerCommandError(
            "Max wissels bereikt.",
            code="max_substitutions",
        )

    player_in = Player.objects.select_related("user").get(id_uuid=new_player_id)
    player_out = Player.objects.select_related("user").get(id_uuid=old_player_id)

    reserve_group = PlayerGroup.objects.get(
        team=team,
        match_data=match_data,
        starting_type__name="Reserve",
    )
    active_group = PlayerGroup.objects.get(
        team=team,
        match_data=match_data,
        players__in=[player_out],
    )

    active_group.players.remove(player_out)
    reserve_group.players.add(player_out)
    reserve_group.players.remove(player_in)
    active_group.players.add(player_in)

    PlayerChange.objects.create(
        player_in=player_in,
        player_out=player_out,
        player_group=active_group,
        match_data=match_data,
        match_part=part_for_event,
        time=datetime.now(UTC),
    )


def _cmd_substitute_against_reg(
    match: Match,
    *,
    match_data: MatchData,
    team: Team,
) -> None:
    """Register an opponent substitution without specifying players.

    Raises:
        TrackerCommandError: If the match is paused or the opponent has reached
            the maximum number of substitutions.

    """
    current_part = _current_part(match_data)
    if current_part and _is_paused(match_data, current_part):
        raise TrackerCommandError(MATCH_IS_PAUSED_MESSAGE, code="match_paused")

    opponent = _other_team(match, team)

    part_for_event = current_part
    if not part_for_event:
        if match_data.status != "active":
            raise TrackerCommandError("Match is not active.", code="match_not_active")
        if match_data.current_part <= 1:
            raise TrackerCommandError(
                NO_ACTIVE_MATCH_PART_MESSAGE,
                code="no_active_part",
            )

        previous_part_number = match_data.current_part - 1
        part_for_event = (
            MatchPart.objects.filter(
                match_data=match_data,
                part_number=previous_part_number,
            )
            .order_by("-start_time")
            .first()
        )
        if not part_for_event:
            part_for_event = (
                MatchPart.objects.filter(match_data=match_data)
                .order_by("-part_number", "-start_time")
                .first()
            )
        if not part_for_event:
            raise TrackerCommandError(
                NO_ACTIVE_MATCH_PART_MESSAGE,
                code="no_active_part",
            )

    substitutions_max = 8
    substitutions_against = PlayerChange.objects.filter(
        match_data=match_data,
        player_group__team=opponent,
    ).count()
    if substitutions_against >= substitutions_max:
        raise TrackerCommandError(
            "Max wissels bereikt.",
            code="max_substitutions",
        )

    opponent_reserve_group = PlayerGroup.objects.get(
        team=opponent,
        match_data=match_data,
        starting_type__name="Reserve",
    )

    PlayerChange.objects.create(
        player_in=None,
        player_out=None,
        player_group=opponent_reserve_group,
        match_data=match_data,
        match_part=part_for_event,
        time=datetime.now(UTC),
    )


def _remove_last_shot(
    event: Shot,
    *,
    match_data: MatchData,
    team: Team,
    opponent: Team,
) -> None:
    scored = event.scored
    event.delete()

    if not scored:
        return

    number_of_goals = Shot.objects.filter(
        match_data=match_data,
        scored=True,
    ).count()
    if number_of_goals % 2 == 1:
        _swap_player_group_types(match_data, team)
        _swap_player_group_types(match_data, opponent)


def _remove_last_player_change(event: PlayerChange, *, match_data: MatchData) -> None:
    # Opponent substitution markers do not have concrete players.
    if not event.player_in or not event.player_out:
        event.delete()
        return

    change_team = event.player_group.team
    reserve_group = PlayerGroup.objects.get(
        team=change_team,
        match_data=match_data,
        starting_type__name="Reserve",
    )
    player_group = PlayerGroup.objects.get(id_uuid=event.player_group.id_uuid)

    player_group.players.remove(event.player_in)
    reserve_group.players.add(event.player_in)

    player_group.players.add(event.player_out)
    reserve_group.players.remove(event.player_out)

    event.delete()


def _remove_last_pause(event: Pause) -> None:
    timeout = Timeout.objects.filter(pause=event).first()
    if timeout:
        timeout.delete()
    if event.active:
        event.delete()
        return

    event.active = True
    cast(Any, event).end_time = None
    event.save(update_fields=["active", "end_time"])


def _remove_last_attack(event: Attack) -> None:
    event.delete()


def _cmd_remove_last_event(match: Match, *, match_data: MatchData, team: Team) -> None:
    opponent = _other_team(match, team)
    event = _get_last_event_model(match_data)
    if not event:
        return

    if isinstance(event, Shot):
        _remove_last_shot(event, match_data=match_data, team=team, opponent=opponent)
        return

    if isinstance(event, PlayerChange):
        _remove_last_player_change(event, match_data=match_data)
        return

    if isinstance(event, Pause):
        _remove_last_pause(event)
        return

    if isinstance(event, Attack):
        _remove_last_attack(event)


def _handle_cmd_start_pause(
    _match: Match,
    *,
    match_data: MatchData,
    team: Team,
    payload: dict[str, Any],
) -> None:
    del team, payload
    _cmd_start_pause(match_data=match_data)


def _handle_cmd_part_end(
    match: Match,
    *,
    match_data: MatchData,
    team: Team,
    payload: dict[str, Any],
) -> None:
    del team, payload
    _cmd_part_end(match, match_data=match_data)


def _handle_cmd_timeout(
    match: Match,
    *,
    match_data: MatchData,
    team: Team,
    payload: dict[str, Any],
) -> None:
    for_team = payload.get("for_team")
    if not isinstance(for_team, bool):
        raise TrackerCommandError("Invalid timeout payload.", code="bad_request")

    timeout_team = team if for_team else _other_team(match, team)
    _cmd_timeout(match, match_data=match_data, team=timeout_team)


def _handle_cmd_new_attack(
    match: Match,
    *,
    match_data: MatchData,
    team: Team,
    payload: dict[str, Any],
) -> None:
    del payload
    _cmd_new_attack(match, match_data=match_data, team=team)


def _handle_cmd_shot_reg(
    match: Match,
    *,
    match_data: MatchData,
    team: Team,
    payload: dict[str, Any],
) -> None:
    player_id = payload.get("player_id")
    for_team = payload.get("for_team")
    shot_type = payload.get("shot_type")
    # Backwards compatibility: some clients might send `goal_type` for shots.
    if shot_type is None:
        shot_type = payload.get("goal_type")

    if not isinstance(player_id, str) or not isinstance(for_team, bool):
        raise TrackerCommandError("Invalid shot_reg payload.", code="bad_request")

    if shot_type is not None and not isinstance(shot_type, str):
        raise TrackerCommandError("Invalid shot type.", code="bad_request")
    _cmd_shot_reg(
        match,
        match_data=match_data,
        team=team,
        params=_ShotRegParams(
            player_id=player_id,
            for_team=for_team,
            shot_type_id=shot_type,
        ),
    )


def _handle_cmd_goal_reg(
    match: Match,
    *,
    match_data: MatchData,
    team: Team,
    payload: dict[str, Any],
) -> None:
    player_id = payload.get("player_id")
    goal_type = payload.get("goal_type")
    for_team = payload.get("for_team")
    if (
        not isinstance(player_id, str)
        or not isinstance(goal_type, str)
        or not isinstance(for_team, bool)
    ):
        raise TrackerCommandError("Invalid goal_reg payload.", code="bad_request")
    _cmd_goal_reg(
        match,
        match_data=match_data,
        team=team,
        params=_GoalRegParams(
            player_id=player_id,
            goal_type_id=goal_type,
            for_team=for_team,
        ),
    )


def _handle_cmd_get_non_active_players(
    _match: Match,
    *,
    match_data: MatchData,
    team: Team,
    payload: dict[str, Any],
) -> None:
    del match_data, team, payload
    # No-op for HTTP; reserve players are included in the state snapshot.


def _handle_cmd_substitute_reg(
    match: Match,
    *,
    match_data: MatchData,
    team: Team,
    payload: dict[str, Any],
) -> None:
    new_player_id = payload.get("new_player_id")
    old_player_id = payload.get("old_player_id")
    if not isinstance(new_player_id, str) or not isinstance(old_player_id, str):
        raise TrackerCommandError("Invalid substitute_reg payload.", code="bad_request")
    _cmd_substitute_reg(
        match,
        match_data=match_data,
        team=team,
        new_player_id=new_player_id,
        old_player_id=old_player_id,
    )


def _handle_cmd_substitute_against_reg(
    match: Match,
    *,
    match_data: MatchData,
    team: Team,
    payload: dict[str, Any],
) -> None:
    del payload
    _cmd_substitute_against_reg(match, match_data=match_data, team=team)


def _handle_cmd_remove_last_event(
    match: Match,
    *,
    match_data: MatchData,
    team: Team,
    payload: dict[str, Any],
) -> None:
    del payload
    _cmd_remove_last_event(match, match_data=match_data, team=team)


_CommandHandler = Callable[[Match], None]


_COMMAND_HANDLERS: dict[str, Callable[..., None]] = {
    "start/pause": _handle_cmd_start_pause,
    "part_end": _handle_cmd_part_end,
    "timeout": _handle_cmd_timeout,
    "new_attack": _handle_cmd_new_attack,
    "shot_reg": _handle_cmd_shot_reg,
    "goal_reg": _handle_cmd_goal_reg,
    "get_non_active_players": _handle_cmd_get_non_active_players,
    "substitute_reg": _handle_cmd_substitute_reg,
    "substitute_against_reg": _handle_cmd_substitute_against_reg,
    "remove_last_event": _handle_cmd_remove_last_event,
}


def _dispatch_command(
    match: Match,
    *,
    match_data: MatchData,
    team: Team,
    payload: dict[str, Any],
) -> None:
    command = payload.get("command")
    if not isinstance(command, str):
        raise TrackerCommandError("Missing command.", code="bad_request")

    handler = _COMMAND_HANDLERS.get(command)
    if not handler:
        raise TrackerCommandError(f"Unknown command: {command}", code="bad_request")

    handler(match, match_data=match_data, team=team, payload=payload)
