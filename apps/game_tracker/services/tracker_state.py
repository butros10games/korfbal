"""Read-optimized tracker snapshots and polling responses."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

from django.db import models
from django.utils import timezone

from apps.game_tracker.domain.match_limits import (
    MAX_SUBSTITUTIONS_PER_TEAM,
    MAX_TIMEOUTS_PER_TEAM,
)
from apps.game_tracker.models import (
    Attack,
    GoalType,
    MatchData,
    MatchPart,
    Pause,
    PlayerChange,
    PlayerGroup,
    PossessionChange,
    Shot,
    Timeout,
)
from apps.game_tracker.services.live_updates import summarize_match_changes
from apps.game_tracker.services.player_groups import RESERVE_GROUP_NAME
from apps.game_tracker.services.tracker_commands.base import (
    TrackerCommandError,
    current_part,
    is_paused,
    other_team,
)
from apps.game_tracker.services.tracker_event_queries import last_event_model
from apps.player.services.goal_song_manifest import build_goal_song_manifest
from apps.schedule.models import Match
from apps.team.models.team import Team


MATCH_TRACKER_DATA_NOT_FOUND = "Match tracker data not found."
_TRACKER_CONFIGURATION_KEYS = frozenset({
    "match_id",
    "match_data_id",
    "team",
    "opponent",
    "goal_types",
    "goal_audio",
    "live_revision",
    "last_changed_at",
})
_EMPTY_PLAYER_STATS = {
    "shots_for": 0,
    "shots_against": 0,
    "goals_for": 0,
    "goals_against": 0,
    "ball_losses": 0,
    "interceptions": 0,
}


def _timer_data(
    match_data: MatchData,
    match_part: MatchPart | None,
) -> dict[str, Any]:
    if match_part is None:
        return {
            "type": "deactivated",
            "match_data_id": str(match_data.id_uuid),
        }

    active_pause = Pause.objects.filter(
        match_data=match_data,
        active=True,
        match_part=match_part,
    ).first()
    pauses = Pause.objects.filter(
        match_data=match_data,
        active=False,
        match_part=match_part,
    )
    base: dict[str, Any] = {
        "match_data_id": str(match_data.id_uuid),
        "time": match_part.start_time.isoformat(),
        "length": match_data.part_length,
        "pause_length": sum(pause.length().total_seconds() for pause in pauses),
        "server_time": datetime.now(UTC).isoformat(),
    }
    if active_pause and active_pause.start_time:
        return {
            **base,
            "type": "pause",
            "calc_to": active_pause.start_time.isoformat(),
        }
    return {**base, "type": "active"}


def _score(match_data: MatchData, *, team: Team, opponent: Team) -> tuple[int, int]:
    totals = (
        Shot.objects
        .filter(match_data=match_data, scored=True, team__in=[team, opponent])
        .values("team")
        .annotate(count=models.Count("id_uuid"))
    )
    goals_by_team = {row["team"]: row["count"] for row in totals}
    return goals_by_team.get(team.id_uuid, 0), goals_by_team.get(opponent.id_uuid, 0)


def _player_stats_by_team(
    match_data: MatchData,
    *,
    team: Team,
    opponent: Team,
) -> dict[str, dict[str, int]]:
    player_stats: dict[str, dict[str, int]] = {}
    shot_totals = (
        Shot.objects
        .filter(match_data=match_data, team__in=[team, opponent])
        .values("player_id", "team_id")
        .annotate(
            shots=models.Count("id_uuid"),
            goals=models.Count("id_uuid", filter=models.Q(scored=True)),
        )
    )
    for row in shot_totals:
        stats = player_stats.setdefault(
            str(row["player_id"]),
            dict(_EMPTY_PLAYER_STATS),
        )
        if row["team_id"] == team.id_uuid:
            stats["shots_for"] = row["shots"]
            stats["goals_for"] = row["goals"]
        else:
            stats["shots_against"] = row["shots"]
            stats["goals_against"] = row["goals"]
    possession_totals = (
        PossessionChange.objects
        .filter(match_data=match_data, team=team)
        .values("player_id", "kind")
        .annotate(count=models.Count("id_uuid"))
    )
    for row in possession_totals:
        if row["player_id"] is None:
            continue
        stats = player_stats.setdefault(
            str(row["player_id"]),
            dict(_EMPTY_PLAYER_STATS),
        )
        field = (
            "ball_losses"
            if row["kind"] == PossessionChange.BALL_LOSS
            else "interceptions"
        )
        stats[field] = row["count"]
    return player_stats


def _player_groups_payload(
    match_data: MatchData,
    *,
    team: Team,
    opponent: Team,
) -> list[dict[str, Any]]:
    player_stats = _player_stats_by_team(match_data, team=team, opponent=opponent)
    player_groups = (
        PlayerGroup.objects
        .select_related("starting_type", "current_type")
        .prefetch_related("players__user")
        .filter(match_data=match_data, team=team)
        .exclude(starting_type__name=RESERVE_GROUP_NAME)
        .order_by("current_type__name", "starting_type__name")
    )
    groups_by_role = {
        role: [group for group in player_groups if group.current_type.name == role]
        for role in ("Aanval", "Verdediging")
    }
    return [
        {
            "id": str(group.id_uuid),
            "starting_type": group.starting_type.name,
            "current_type": group.current_type.name,
            "players": [
                {
                    "id": str(player.id_uuid),
                    "name": player.user.username,
                    **player_stats.get(str(player.id_uuid), _EMPTY_PLAYER_STATS),
                }
                for player in group.players.all()
            ],
        }
        for role in ("Aanval", "Verdediging")
        for group in groups_by_role[role]
    ]


def _reserve_players_payload(
    match_data: MatchData,
    *,
    team: Team,
) -> list[dict[str, Any]]:
    reserve_group = (
        PlayerGroup.objects
        .prefetch_related("players__user")
        .filter(
            match_data=match_data,
            team=team,
            starting_type__name=RESERVE_GROUP_NAME,
        )
        .first()
    )
    if reserve_group is None:
        return []
    return [
        {"id": str(player.id_uuid), "name": player.user.username}
        for player in reserve_group.players.all()
    ]


def _last_event_payload(
    match_data: MatchData,
    *,
    team: Team,
    opponent: Team,
) -> dict[str, Any]:
    event = last_event_model(match_data)
    if event is None:
        return {"type": "no_event"}
    if isinstance(event, Shot):
        goals_for, goals_against = _score(
            match_data,
            team=team,
            opponent=opponent,
        )
        return _serialize_last_event_shot(
            event,
            team=team,
            goals_for=goals_for,
            goals_against=goals_against,
        )
    payload: dict[str, Any]
    if isinstance(event, PlayerChange):
        payload = _serialize_last_event_player_change(event)
    elif isinstance(event, PossessionChange):
        payload = _serialize_last_event_possession_change(event, team=team)
    elif isinstance(event, Pause):
        payload = _serialize_last_event_pause(event)
    elif isinstance(event, Attack):
        payload = _serialize_last_event_attack(event)
    else:
        payload = {"type": "no_event"}
    return payload


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
    return {**common, "type": "shot", "name": "Schot"}


def _serialize_last_event_player_change(event: PlayerChange) -> dict[str, Any]:
    if not event.time:
        return {"type": "no_event"}
    common = {
        "type": "substitute",
        "id": str(event.id_uuid),
        "player_group_id": str(event.player_group.id_uuid),
        "time_iso": event.time.isoformat(),
        "time": event.time.isoformat(),
    }
    if not event.player_in or not event.player_out:
        return {
            **common,
            "name": "Wissel tegenstander",
            "player_in": None,
            "player_in_id": None,
            "player_out": None,
            "player_out_id": None,
        }
    return {
        **common,
        "name": "Wissel",
        "player_in": event.player_in.user.username,
        "player_in_id": str(event.player_in.id_uuid),
        "player_out": event.player_out.user.username,
        "player_out_id": str(event.player_out.id_uuid),
    }


def _serialize_last_event_possession_change(
    event: PossessionChange,
    *,
    team: Team,
) -> dict[str, Any]:
    return {
        "type": "possession_change",
        "id": str(event.id_uuid),
        "name": (
            "Balverlies"
            if event.kind == PossessionChange.BALL_LOSS
            else "Onderschepping"
        ),
        "kind": event.kind,
        "player": event.player.user.username if event.player else None,
        "player_id": str(event.player_id) if event.player_id else None,
        "for_team": event.team_id == team.id_uuid,
        "team_id": str(event.team_id),
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


def get_tracker_state(match: Match, *, team: Team) -> dict[str, Any]:
    """Return a snapshot of the current tracker state.

    Raises:
        TrackerCommandError: If tracker data does not exist for the match.

    """
    opponent = other_team(match, team)
    match_data = MatchData.objects.filter(match_link=match).first()
    if match_data is None:
        raise TrackerCommandError(MATCH_TRACKER_DATA_NOT_FOUND, code="not_found")

    match_part = current_part(match_data)
    goals_for, goals_against = _score(match_data, team=team, opponent=opponent)
    paused = is_paused(match_data, match_part)
    substitutions_by_team = {
        row["player_group__team"]: row["count"]
        for row in (
            PlayerChange.objects
            .filter(match_data=match_data, player_group__team__in=[team, opponent])
            .values("player_group__team")
            .annotate(count=models.Count("id_uuid"))
        )
    }
    substitutions_for = substitutions_by_team.get(team.id_uuid, 0)
    substitutions_against = substitutions_by_team.get(opponent.id_uuid, 0)
    timeouts_by_team = {
        row["team"]: row["count"]
        for row in (
            Timeout.objects
            .filter(match_data=match_data, team__in=[team, opponent])
            .values("team")
            .annotate(count=models.Count("id_uuid"))
        )
    }
    player_groups = _player_groups_payload(
        match_data,
        team=team,
        opponent=opponent,
    )
    reserve_players = _reserve_players_payload(match_data, team=team)
    player_ids = [
        player["id"] for group in player_groups for player in group["players"]
    ]
    player_ids.extend(player["id"] for player in reserve_players)

    return {
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
        "score": {"for": goals_for, "against": goals_against},
        "substitutions": {
            "for": substitutions_for,
            "against": substitutions_against,
            "max": MAX_SUBSTITUTIONS_PER_TEAM,
        },
        "timeouts": {
            "for": timeouts_by_team.get(team.id_uuid, 0),
            "against": timeouts_by_team.get(opponent.id_uuid, 0),
            "max": MAX_TIMEOUTS_PER_TEAM,
        },
        "substitutions_total": substitutions_for + substitutions_against,
        "paused": paused,
        "start_stop_label": (
            "Pauze" if match_data.status == "active" and not paused else "Start"
        ),
        "timer": _timer_data(match_data, match_part),
        "player_groups": player_groups,
        "reserve_players": reserve_players,
        "goal_audio": build_goal_song_manifest(
            player_ids=player_ids,
            team=team,
            season=match.season,
        ),
        "goal_types": [
            {"id": str(goal_type.id_uuid), "name": goal_type.name}
            for goal_type in GoalType.objects.order_by("name")
        ],
        "last_event": _last_event_payload(
            match_data,
            team=team,
            opponent=opponent,
        ),
        "last_changed_at": match_data.live_changed_at.isoformat(),
        "live_revision": match_data.live_revision,
        "command_sequence": match_data.command_sequence,
    }


def compact_tracker_state(
    state: dict[str, Any],
    *,
    resources: list[str],
) -> dict[str, Any]:
    """Return a patch that reuses configuration from the initial snapshot."""
    return {
        "changed": True,
        "live_revision": state["live_revision"],
        "last_changed_at": state["last_changed_at"],
        "resources": resources,
        "patch": {
            key: value
            for key, value in state.items()
            if key not in _TRACKER_CONFIGURATION_KEYS and key != "resources"
        },
    }


def poll_tracker_state(
    match: Match,
    *,
    team: Team,
    since_revision: int,
    timeout_seconds: int = 25,
    compact: bool = False,
) -> dict[str, Any]:
    """Return changed tracker state without occupying a request worker.

    The timeout remains for wire compatibility. SSE and interval polling own
    waiting, so blocking a Django request here would only consume a worker.

    Raises:
        TrackerCommandError: If tracker data does not exist for the match.

    """
    del timeout_seconds
    other_team(match, team)

    match_data = MatchData.objects.filter(match_link=match).first()
    if match_data is None:
        raise TrackerCommandError(MATCH_TRACKER_DATA_NOT_FOUND, code="not_found")
    if match_data.live_revision > since_revision:
        state = get_tracker_state(match, team=team)
        summary = summarize_match_changes(
            MatchData.objects.get(pk=match_data.pk),
            since_revision=since_revision,
        )
        resources = sorted(resource.value for resource in summary.resources)
        if compact:
            return compact_tracker_state(state, resources=resources)
        state["resources"] = resources
        return state
    return {
        "changed": False,
        "server_time": timezone.now().isoformat(),
        "last_changed_at": match_data.live_changed_at.isoformat(),
        "live_revision": match_data.live_revision,
    }
