"""Query service for team overview and reporting endpoints."""

from __future__ import annotations

from django.db import models
from django.db.models import Exists, F, OuterRef, Q, QuerySet

from apps.game_tracker.models import MatchData, MatchPlayer, PlayerMatchImpact, Shot
from apps.player.models import Player
from apps.schedule.models import Match, Season
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
    return Season.objects.filter(
        Q(pk__in=TeamData.objects.filter(team=team).values("season_id"))
        | Q(pk__in=Match.objects.filter(home_team=team).values("season_id"))
        | Q(pk__in=Match.objects.filter(away_team=team).values("season_id"))
    ).order_by("-start_date")


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

    # Keep match membership in the database instead of transferring every match
    # UUID into Python and repeating that growing list in the player query.
    match_ids = matches.order_by().values("id_uuid")
    player_ids = player_ids.union(
        MatchPlayer.objects.filter(
            team=team,
            match_data_id__in=match_ids,
        ).values_list("player_id", flat=True),
        # Conceded shots name the shooting team but retain the opposing
        # defender as their player. Resolve that player's side of the match.
        Shot.objects.filter(
            Q(team=team, for_team=True)
            | Q(
                for_team=False,
                match_data__match_link__home_team=team,
                team=F("match_data__match_link__away_team"),
            )
            | Q(
                for_team=False,
                match_data__match_link__away_team=team,
                team=F("match_data__match_link__home_team"),
            ),
            match_data_id__in=match_ids,
        ).values_list("player_id", flat=True),
    )

    return (
        Player.objects
        .select_related("user")
        .prefetch_related("goal_song_selections")
        .only(
            "id_uuid",
            "profile_picture",
            "profile_picture_visibility",
            "stats_visibility",
            "goal_song_uri",
            "song_start_time",
            "user__username",
            "name",
            "knkv_person_id",
            "knkv_privacy",
            "archived_at",
            "knkv_observed_at",
        )
        .filter(id_uuid__in=player_ids)
        .order_by("user__username", "name", "id_uuid")
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


def player_impact_matches(
    *,
    team: Team,
    season: Season | None,
    player: Player,
    algorithm_version: str,
) -> QuerySet[MatchData]:
    """Prefer persisted impacts, otherwise discover designated or shooting players."""
    matches = team_matches(team, season).filter(status="finished")
    has_impact = Exists(
        PlayerMatchImpact.objects.filter(
            match_data_id=OuterRef("pk"),
            player=player,
            algorithm_version=algorithm_version,
        )
    )
    persisted = matches.filter(has_impact)
    if persisted.exists():
        return persisted

    # Match participation is an existence check, not a join of every player's
    # designation, shot and impact. Those independent event sets can otherwise
    # multiply into thousands of intermediate rows per match before DISTINCT.
    return matches.filter(
        has_impact
        | Exists(
            MatchPlayer.objects.filter(match_data_id=OuterRef("pk"), player=player)
        )
        | Exists(Shot.objects.filter(match_data_id=OuterRef("pk"), player=player))
    )


def team_data_for_season(*, team: Team, season: Season | None) -> TeamData | None:
    """Return the selected season roster, or the latest roster when no season is set."""
    queryset = TeamData.objects.filter(team=team)
    if season is not None:
        queryset = queryset.filter(season=season)
    return queryset.order_by("-season__start_date").first()
