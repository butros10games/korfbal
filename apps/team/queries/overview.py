"""Query service for team overview and reporting endpoints."""

from __future__ import annotations

from django.db import models
from django.db.models import Q, QuerySet

from apps.game_tracker.models import MatchData, MatchPlayer, Shot
from apps.player.models import Player
from apps.schedule.models import Season
from apps.schedule.queries.seasons import (
    current_season,
    most_recent_season,
    requested_or_default_season,
)
from apps.team.models.team import Team
from apps.team.models.team_data import TeamData


def resolve_team_season(
    requested_id: str | None,
    seasons: list[Season],
) -> Season | None:
    """Resolve a team-scoped season without broadening invalid requests."""
    return requested_or_default_season(requested_id, seasons) or (
        current_season() or most_recent_season()
    )


def team_seasons(team: Team) -> QuerySet[Season]:
    """Return seasons with a roster or match connected to the team."""
    return (
        Season.objects
        .filter(
            Q(team_data__team=team)
            | Q(matches__home_team=team)
            | Q(matches__away_team=team)
        )
        .distinct()
        .order_by("-start_date")
    )


def team_matches(team: Team, season: Season | None) -> QuerySet[MatchData]:
    """Return match data for a team, optionally scoped to one season."""
    queryset = (
        MatchData.objects
        .select_related(
            "match_link",
            "match_link__home_team",
            "match_link__home_team__club",
            "match_link__away_team",
            "match_link__away_team__club",
            "match_link__season",
        )
        .filter(Q(match_link__home_team=team) | Q(match_link__away_team=team))
        .fetch_mode(models.FETCH_RAISE)
    )
    return queryset.filter(match_link__season=season) if season else queryset


def team_players(
    team: Team,
    season: Season | None,
    matches: QuerySet[MatchData],
) -> QuerySet[Player]:
    """Return players observed in rosters, matches, or shots."""
    team_data = TeamData.objects.filter(team=team)
    if season is not None:
        team_data = team_data.filter(season=season)
    player_ids = TeamData.players.through.objects.filter(
        teamdata_id__in=team_data.values_list("id", flat=True),
    ).values_list("player_id", flat=True)

    match_ids = list(matches.values_list("id_uuid", flat=True))
    if match_ids:
        player_ids = player_ids.union(
            MatchPlayer.objects.filter(
                team=team,
                match_data_id__in=match_ids,
            ).values_list("player_id", flat=True),
            Shot.objects.filter(
                team=team,
                match_data_id__in=match_ids,
            ).values_list("player_id", flat=True),
        )

    return (
        Player.objects
        .select_related("user")
        .only(
            "id_uuid",
            "profile_picture",
            "profile_picture_visibility",
            "stats_visibility",
            "goal_song_uri",
            "song_start_time",
            "goal_song_song_ids",
            "user__username",
        )
        .filter(id_uuid__in=player_ids)
        .order_by("user__username", "id_uuid")
        .fetch_mode(models.FETCH_RAISE)
    )


def main_roster_ids(*, team: Team, season: Season | None) -> set[str]:
    """Return player IDs explicitly assigned to the season roster."""
    team_data = TeamData.objects.filter(team=team)
    if season is not None:
        team_data = team_data.filter(season=season)
    return {
        str(player_id)
        for player_id in (
            team_data
            .values_list("players__id_uuid", flat=True)
            .distinct()
            .exclude(players__id_uuid__isnull=True)
        )
    }
