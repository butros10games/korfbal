"""Helper functions for building match statistics payloads."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, TypedDict

from django.db.models import Count, Q

from apps.game_tracker.models import (
    GoalType,
    MatchData,
    MatchPlayer,
    PlayerGroup,
    PossessionChange,
    Shot,
)
from apps.player.models.player import Player
from apps.schedule.models import Match
from apps.team.models.team import Team
from apps.team.models.team_data import TeamData


@dataclass(frozen=True)
class _MatchStatsContext:
    match: Match
    match_data: MatchData
    home_team: Team
    away_team: Team


def _build_general_stats(
    *,
    match_data: MatchData,
    home_team: Team,
    away_team: Team,
    goal_types: list[GoalType],
) -> dict[str, object]:
    """Build totals and goal-type breakdowns from two grouped event queries."""
    teams = (("for", home_team), ("against", away_team))
    shots = list(
        Shot.objects
        .filter(match_data=match_data)
        .values("team_id", "shot_type_id")
        .annotate(shots=Count("pk"), goals=Count("pk", filter=Q(scored=True)))
        .order_by()
    )
    goals_by_type = {
        (row["team_id"], row["shot_type_id"]): row["goals"] for row in shots
    }
    possessions = {
        (row["team_id"], row["kind"]): row["count"]
        for row in PossessionChange.objects
        .filter(match_data=match_data)
        .values("team_id", "kind")
        .annotate(count=Count("pk"))
        .order_by()
    }
    return {
        **{
            f"{metric}_{side}": sum(
                row[metric] for row in shots if row["team_id"] == team.pk
            )
            for side, team in teams
            for metric in ("shots", "goals")
        },
        **{
            f"{metric}_{side}": possessions.get((team.pk, kind), 0)
            for side, team in teams
            for metric, kind in (
                ("ball_losses", PossessionChange.BALL_LOSS),
                ("interceptions", PossessionChange.INTERCEPTION),
            )
        },
        "team_goal_stats": {
            goal_type.name: {
                "goals_by_player": goals_by_type.get((home_team.pk, goal_type.pk), 0),
                "goals_against_player": goals_by_type.get(
                    (away_team.pk, goal_type.pk), 0
                ),
            }
            for goal_type in goal_types
        },
        "goal_types": [
            {"id": str(goal_type.pk), "name": goal_type.name}
            for goal_type in goal_types
        ],
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
        Player.objects
        .select_related("user")
        .filter(id_uuid__in=player_ids)
        .annotate(
            shots_for=Count(
                "shots__id_uuid",
                distinct=True,
                filter=Q(
                    shots__match_data=match_data,
                    shots__team=team,
                ),
            ),
            shots_against=Count(
                "shots__id_uuid",
                distinct=True,
                filter=Q(
                    shots__match_data=match_data,
                    shots__team=other_team,
                ),
            ),
            goals_for=Count(
                "shots__id_uuid",
                distinct=True,
                filter=Q(
                    shots__match_data=match_data,
                    shots__team=team,
                    shots__scored=True,
                ),
            ),
            goals_against=Count(
                "shots__id_uuid",
                distinct=True,
                filter=Q(
                    shots__match_data=match_data,
                    shots__team=other_team,
                    shots__scored=True,
                ),
            ),
            ball_losses=Count(
                "possession_changes__id_uuid",
                distinct=True,
                filter=Q(
                    possession_changes__match_data=match_data,
                    possession_changes__team=team,
                    possession_changes__kind=PossessionChange.BALL_LOSS,
                ),
            ),
            interceptions=Count(
                "possession_changes__id_uuid",
                distinct=True,
                filter=Q(
                    possession_changes__match_data=match_data,
                    possession_changes__team=team,
                    possession_changes__kind=PossessionChange.INTERCEPTION,
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
            "ball_losses": int(getattr(player, "ball_losses", 0)),
            "interceptions": int(getattr(player, "interceptions", 0)),
        }
        for player in queryset
    ]


def _match_roster_player_ids(*, match_data: MatchData, team: Team) -> set[str]:
    return {
        str(player_id)
        for player_id in MatchPlayer.objects
        .filter(match_data=match_data, team=team)
        .values_list("player__id_uuid", flat=True)
        .distinct()
    }


def _match_shot_player_ids(*, match_data: MatchData, team: Team) -> set[str]:
    return {
        str(player_id)
        for player_id in Shot.objects
        .filter(match_data=match_data, team=team)
        .values_list("player__id_uuid", flat=True)
        .distinct()
    }


class _ShotOnlySideInputs(TypedDict):
    home_group_ids: set[str]
    away_group_ids: set[str]
    home_teamdata_ids: set[str]
    away_teamdata_ids: set[str]
    shot_home_ids: set[str]
    shot_away_ids: set[str]


def _resolve_shot_only_player_side(
    *,
    ctx: _MatchStatsContext,
    player_id: str,
    inputs: _ShotOnlySideInputs,
) -> str:
    in_home_groups = player_id in inputs["home_group_ids"]
    in_away_groups = player_id in inputs["away_group_ids"]
    if in_home_groups != in_away_groups:
        side = "home" if in_home_groups else "away"

    else:
        in_home_teamdata = player_id in inputs["home_teamdata_ids"]
        in_away_teamdata = player_id in inputs["away_teamdata_ids"]
        if in_home_teamdata != in_away_teamdata:
            side = "home" if in_home_teamdata else "away"

        else:
            in_home_shots = player_id in inputs["shot_home_ids"]
            in_away_shots = player_id in inputs["shot_away_ids"]
            if in_home_shots != in_away_shots:
                side = "home" if in_home_shots else "away"
            else:
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

                side = "home" if home_count >= away_count else "away"
    return side


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

    # Prefer per-match team assignment when available.
    # PlayerGroup membership is created/edited during match tracking and preserves
    # the historical “this player belonged to this team in this match” intent.
    home_group_ids = set(
        PlayerGroup.objects
        .filter(
            match_data=ctx.match_data,
            team=ctx.home_team,
            players__id_uuid__in=shot_only_ids,
        )
        .values_list("players__id_uuid", flat=True)
        .distinct()
    )
    away_group_ids = set(
        PlayerGroup.objects
        .filter(
            match_data=ctx.match_data,
            team=ctx.away_team,
            players__id_uuid__in=shot_only_ids,
        )
        .values_list("players__id_uuid", flat=True)
        .distinct()
    )

    home_group_ids_str = {str(player_id) for player_id in home_group_ids}
    away_group_ids_str = {str(player_id) for player_id in away_group_ids}

    home_teamdata_ids = set(
        TeamData.objects
        .filter(
            team=ctx.home_team,
            season=ctx.match.season,
            players__id_uuid__in=shot_only_ids,
        )
        .values_list("players__id_uuid", flat=True)
        .distinct()
    )
    away_teamdata_ids = set(
        TeamData.objects
        .filter(
            team=ctx.away_team,
            season=ctx.match.season,
            players__id_uuid__in=shot_only_ids,
        )
        .values_list("players__id_uuid", flat=True)
        .distinct()
    )

    home_teamdata_ids_str = {str(player_id) for player_id in home_teamdata_ids}
    away_teamdata_ids_str = {str(player_id) for player_id in away_teamdata_ids}

    side_inputs: _ShotOnlySideInputs = {
        "home_group_ids": home_group_ids_str,
        "away_group_ids": away_group_ids_str,
        "home_teamdata_ids": home_teamdata_ids_str,
        "away_teamdata_ids": away_teamdata_ids_str,
        "shot_home_ids": shot_home_ids,
        "shot_away_ids": shot_away_ids,
    }

    for player_id in shot_only_ids:
        side = _resolve_shot_only_player_side(
            ctx=ctx,
            player_id=player_id,
            inputs=side_inputs,
        )
        if side == "home":
            home_player_ids.add(player_id)
        else:
            away_player_ids.add(player_id)


def build_match_stats_payload(
    *,
    match: Match,
    match_data: MatchData,
) -> dict[str, Any]:
    """Build match statistics and resolve players to their tracked side."""
    home_team = match.home_team
    away_team = match.away_team

    ctx = _MatchStatsContext(
        match=match,
        match_data=match_data,
        home_team=home_team,
        away_team=away_team,
    )

    general = _build_general_stats(
        match_data=match_data,
        home_team=home_team,
        away_team=away_team,
        goal_types=list(GoalType.objects.all()),
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
