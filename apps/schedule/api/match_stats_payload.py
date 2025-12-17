"""Helper functions for building match statistics payloads."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from django.db.models import Count, Q

from apps.game_tracker.models import GoalType, MatchData, MatchPlayer, Shot
from apps.player.models.player import Player
from apps.schedule.models import Match
from apps.team.models.team import Team
from apps.team.models.team_data import TeamData


def _goal_types_json(goal_types: list[GoalType]) -> list[dict[str, str]]:
    return [
        {"id": str(goal_type.id_uuid), "name": goal_type.name}
        for goal_type in goal_types
    ]


@dataclass(frozen=True)
class _MatchStatsContext:
    match: Match
    match_data: MatchData
    home_team: Team
    away_team: Team


def _build_team_goal_stats(
    *,
    match_data: MatchData,
    home_team: Team,
    away_team: Team,
    goal_types: list[GoalType],
) -> dict[str, dict[str, int]]:
    team_goal_stats: dict[str, dict[str, int]] = {}
    for goal_type in goal_types:
        goals_home = Shot.objects.filter(
            match_data=match_data,
            team=home_team,
            shot_type=goal_type,
            scored=True,
        ).count()
        goals_away = Shot.objects.filter(
            match_data=match_data,
            team=away_team,
            shot_type=goal_type,
            scored=True,
        ).count()
        team_goal_stats[goal_type.name] = {
            "goals_by_player": int(goals_home),
            "goals_against_player": int(goals_away),
        }

    return team_goal_stats


def _build_general_stats(
    *,
    match_data: MatchData,
    home_team: Team,
    away_team: Team,
    team_goal_stats: dict[str, dict[str, int]],
    goal_types_json: list[dict[str, str]],
) -> dict[str, object]:
    return {
        "shots_for": Shot.objects.filter(
            match_data=match_data,
            team=home_team,
        ).count(),
        "shots_against": Shot.objects.filter(
            match_data=match_data,
            team=away_team,
        ).count(),
        "goals_for": Shot.objects.filter(
            match_data=match_data,
            team=home_team,
            scored=True,
        ).count(),
        "goals_against": Shot.objects.filter(
            match_data=match_data,
            team=away_team,
            scored=True,
        ).count(),
        "team_goal_stats": team_goal_stats,
        "goal_types": goal_types_json,
    }


def _build_player_lines(
    *,
    match_data: MatchData,
    player_ids: set[str],
    team: Team,
    other_team: Team,
) -> list[dict[str, object]]:
    if not player_ids:
        return []

    queryset = (
        Player.objects.select_related("user")
        .filter(id_uuid__in=player_ids)
        .annotate(
            shots_for=Count(
                "shots__id_uuid",
                filter=Q(
                    shots__match_data=match_data,
                    shots__team=team,
                ),
            ),
            shots_against=Count(
                "shots__id_uuid",
                filter=Q(
                    shots__match_data=match_data,
                    shots__team=other_team,
                ),
            ),
            goals_for=Count(
                "shots__id_uuid",
                filter=Q(
                    shots__match_data=match_data,
                    shots__team=team,
                    shots__scored=True,
                ),
            ),
            goals_against=Count(
                "shots__id_uuid",
                filter=Q(
                    shots__match_data=match_data,
                    shots__team=other_team,
                    shots__scored=True,
                ),
            ),
        )
        .order_by("-goals_for", "-shots_for", "user__username")
    )

    return [
        {
            "id_uuid": str(player.id_uuid),
            "display_name": player.user.get_full_name() or player.user.username,
            "username": player.user.username,
            "profile_picture_url": player.get_profile_picture(),
            "profile_url": player.get_absolute_url(),
            "shots_for": int(getattr(player, "shots_for", 0)),
            "shots_against": int(getattr(player, "shots_against", 0)),
            "goals_for": int(getattr(player, "goals_for", 0)),
            "goals_against": int(getattr(player, "goals_against", 0)),
        }
        for player in queryset
    ]


def _match_roster_player_ids(*, match_data: MatchData, team: Team) -> set[str]:
    return {
        str(player_id)
        for player_id in MatchPlayer.objects.filter(match_data=match_data, team=team)
        .values_list("player__id_uuid", flat=True)
        .distinct()
    }


def _match_shot_player_ids(*, match_data: MatchData, team: Team) -> set[str]:
    return {
        str(player_id)
        for player_id in Shot.objects.filter(match_data=match_data, team=team)
        .values_list("player__id_uuid", flat=True)
        .distinct()
    }


def _assign_shot_only_players(
    *,
    ctx: _MatchStatsContext,
    home_player_ids: set[str],
    away_player_ids: set[str],
    shot_home_ids: set[str],
    shot_away_ids: set[str],
) -> None:
    shot_only_ids = (shot_home_ids | shot_away_ids) - home_player_ids - away_player_ids
    if not shot_only_ids:
        return

    home_teamdata_ids = set(
        TeamData.objects.filter(
            team=ctx.home_team,
            season=ctx.match.season,
            players__id_uuid__in=shot_only_ids,
        )
        .values_list("players__id_uuid", flat=True)
        .distinct()
    )
    away_teamdata_ids = set(
        TeamData.objects.filter(
            team=ctx.away_team,
            season=ctx.match.season,
            players__id_uuid__in=shot_only_ids,
        )
        .values_list("players__id_uuid", flat=True)
        .distinct()
    )

    home_teamdata_ids_str = {str(player_id) for player_id in home_teamdata_ids}
    away_teamdata_ids_str = {str(player_id) for player_id in away_teamdata_ids}

    for player_id in shot_only_ids:
        in_home_teamdata = player_id in home_teamdata_ids_str
        in_away_teamdata = player_id in away_teamdata_ids_str

        if in_home_teamdata and not in_away_teamdata:
            home_player_ids.add(player_id)
            continue

        if in_away_teamdata and not in_home_teamdata:
            away_player_ids.add(player_id)
            continue

        in_home_shots = player_id in shot_home_ids
        in_away_shots = player_id in shot_away_ids

        if in_home_shots and not in_away_shots:
            home_player_ids.add(player_id)
            continue

        if in_away_shots and not in_home_shots:
            away_player_ids.add(player_id)
            continue

        home_count = Shot.objects.filter(
            match_data=ctx.match_data,
            team=ctx.home_team,
            player__id_uuid=player_id,
        ).count()
        away_count = Shot.objects.filter(
            match_data=ctx.match_data,
            team=ctx.away_team,
            player__id_uuid=player_id,
        ).count()

        if home_count >= away_count:
            home_player_ids.add(player_id)
        else:
            away_player_ids.add(player_id)


def _build_match_stats_payload(
    *,
    match: Match,
    match_data: MatchData,
) -> dict[str, Any]:
    home_team = match.home_team
    away_team = match.away_team

    ctx = _MatchStatsContext(
        match=match,
        match_data=match_data,
        home_team=home_team,
        away_team=away_team,
    )

    goal_types = list(GoalType.objects.all())
    goal_types_json = _goal_types_json(goal_types)

    team_goal_stats = _build_team_goal_stats(
        match_data=match_data,
        home_team=home_team,
        away_team=away_team,
        goal_types=goal_types,
    )

    general = _build_general_stats(
        match_data=match_data,
        home_team=home_team,
        away_team=away_team,
        team_goal_stats=team_goal_stats,
        goal_types_json=goal_types_json,
    )

    home_player_ids = _match_roster_player_ids(match_data=match_data, team=home_team)
    away_player_ids = _match_roster_player_ids(match_data=match_data, team=away_team)

    shot_home_ids = _match_shot_player_ids(match_data=match_data, team=home_team)
    shot_away_ids = _match_shot_player_ids(match_data=match_data, team=away_team)

    _assign_shot_only_players(
        ctx=ctx,
        home_player_ids=home_player_ids,
        away_player_ids=away_player_ids,
        shot_home_ids=shot_home_ids,
        shot_away_ids=shot_away_ids,
    )

    players_payload = {
        "home": _build_player_lines(
            match_data=match_data,
            player_ids=home_player_ids,
            team=home_team,
            other_team=away_team,
        ),
        "away": _build_player_lines(
            match_data=match_data,
            player_ids=away_player_ids,
            team=away_team,
            other_team=home_team,
        ),
    }

    return {
        "general": general,
        "players": players_payload,
        "meta": {
            "home_team_id": str(home_team.id_uuid),
            "away_team_id": str(away_team.id_uuid),
        },
    }
