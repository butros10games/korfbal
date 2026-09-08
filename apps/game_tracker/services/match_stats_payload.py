"""Helper functions for building match statistics payloads."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, TypedDict, cast
from uuid import UUID

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


class _ShotTotals(TypedDict):
    team_id: UUID | None
    player_id: UUID | None
    shot_type_id: UUID | None
    shots: int
    goals: int


class _PossessionTotals(TypedDict):
    team_id: UUID | None
    player_id: UUID | None
    kind: str
    count: int


@dataclass(frozen=True)
class _MatchStatsContext:
    match: Match
    match_data: MatchData
    home_team: Team
    away_team: Team
    shots: list[_ShotTotals]
    possessions: list[_PossessionTotals]


def _build_general_stats(
    *,
    shots: list[_ShotTotals],
    possessions: list[_PossessionTotals],
    home_team: Team,
    away_team: Team,
    goal_types: list[GoalType],
) -> dict[str, object]:
    """Build totals and goal-type breakdowns from two grouped event queries."""
    teams = (("for", home_team), ("against", away_team))
    goals_by_type: dict[tuple[UUID | None, UUID | None], int] = {}
    for row in shots:
        key = (row["team_id"], row["shot_type_id"])
        goals_by_type[key] = goals_by_type.get(key, 0) + row["goals"]
    possession_counts: dict[tuple[UUID | None, str], int] = {}
    for possession in possessions:
        key = (possession["team_id"], possession["kind"])
        possession_counts[key] = possession_counts.get(key, 0) + possession["count"]
    return {
        **{
            f"{metric}_{side}": sum(
                row[metric] for row in shots if row["team_id"] == team.pk
            )
            for side, team in teams
            for metric in ("shots", "goals")
        },
        **{
            f"{metric}_{side}": possession_counts.get((team.pk, kind), 0)
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
    ctx: _MatchStatsContext,
    player_ids: set[str],
    team: Team,
    other_team: Team,
) -> list[dict[str, object]]:
    if not player_ids:
        return []

    totals: dict[str, dict[str, int]] = {
        player_id: dict.fromkeys(
            (
                "shots_for",
                "shots_against",
                "goals_for",
                "goals_against",
                "ball_losses",
                "interceptions",
            ),
            0,
        )
        for player_id in player_ids
    }
    for shot in ctx.shots:
        player_totals = totals.get(str(shot["player_id"]))
        if player_totals is None or shot["team_id"] not in {team.pk, other_team.pk}:
            continue
        side = "for" if shot["team_id"] == team.pk else "against"
        for metric in ("shots", "goals"):
            player_totals[f"{metric}_{side}"] += shot[metric]
    for possession in ctx.possessions:
        player_totals = totals.get(str(possession["player_id"]))
        if player_totals is None or possession["team_id"] != team.pk:
            continue
        metric = {
            PossessionChange.BALL_LOSS: "ball_losses",
            PossessionChange.INTERCEPTION: "interceptions",
        }.get(possession["kind"])
        if metric:
            player_totals[metric] += possession["count"]

    queryset = (
        Player.objects
        .select_related("user")
        .filter(id_uuid__in=player_ids)
        .order_by("user__username")
    )
    players = sorted(
        queryset,
        key=lambda player: (
            -totals[str(player.pk)]["goals_for"],
            -totals[str(player.pk)]["shots_for"],
        ),
    )

    return [
        {
            "id_uuid": str(player.id_uuid),
            "display_name": (
                player.user.get_full_name() if player.user_id else player.display_name
            )
            or player.display_name,
            "username": player.display_name,
            "profile_picture_url": player.get_profile_picture(),
            "profile_url": player.get_absolute_url(),
            **totals[str(player.pk)],
        }
        for player in players
    ]


def _match_roster_player_ids(*, match_data: MatchData, team: Team) -> set[str]:
    return {
        str(player_id)
        for player_id in MatchPlayer.objects
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
    shot_counts: dict[tuple[str, str], int]


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
                home_count = inputs["shot_counts"].get(
                    (player_id, str(ctx.home_team.pk)), 0
                )
                away_count = inputs["shot_counts"].get(
                    (player_id, str(ctx.away_team.pk)), 0
                )

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

    shot_counts: dict[tuple[str, str], int] = {}
    for row in ctx.shots:
        key = (str(row["player_id"]), str(row["team_id"]))
        shot_counts[key] = shot_counts.get(key, 0) + row["shots"]

    side_inputs: _ShotOnlySideInputs = {
        "home_group_ids": home_group_ids_str,
        "away_group_ids": away_group_ids_str,
        "home_teamdata_ids": home_teamdata_ids_str,
        "away_teamdata_ids": away_teamdata_ids_str,
        "shot_home_ids": shot_home_ids,
        "shot_away_ids": shot_away_ids,
        "shot_counts": shot_counts,
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
        shots=cast(
            list[_ShotTotals],
            list(
                Shot.objects
                .filter(match_data=match_data)
                .values("team_id", "player_id", "shot_type_id")
                .annotate(shots=Count("pk"), goals=Count("pk", filter=Q(scored=True)))
                .order_by()
            ),
        ),
        possessions=cast(
            list[_PossessionTotals],
            list(
                PossessionChange.objects
                .filter(match_data=match_data)
                .values("team_id", "player_id", "kind")
                .annotate(count=Count("pk"))
                .order_by()
            ),
        ),
    )

    general = _build_general_stats(
        shots=ctx.shots,
        possessions=ctx.possessions,
        home_team=home_team,
        away_team=away_team,
        goal_types=list(GoalType.objects.all()),
    )

    home_player_ids = _match_roster_player_ids(match_data=match_data, team=home_team)
    away_player_ids = _match_roster_player_ids(match_data=match_data, team=away_team)

    shot_home_ids = {
        str(row["player_id"])
        for row in ctx.shots
        if row["team_id"] == home_team.pk and row["player_id"] is not None
    }
    shot_away_ids = {
        str(row["player_id"])
        for row in ctx.shots
        if row["team_id"] == away_team.pk and row["player_id"] is not None
    }

    _assign_shot_only_players(
        ctx=ctx,
        home_player_ids=home_player_ids,
        away_player_ids=away_player_ids,
        shot_home_ids=shot_home_ids,
        shot_away_ids=shot_away_ids,
    )

    players_payload = {
        "home": _build_player_lines(
            ctx=ctx,
            player_ids=home_player_ids,
            team=home_team,
            other_team=away_team,
        ),
        "away": _build_player_lines(
            ctx=ctx,
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
