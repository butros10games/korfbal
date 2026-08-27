"""Query service for club overview and eligibility screens."""

from __future__ import annotations

from django.db import models
from django.db.models import Q, QuerySet

from apps.club.models.club import Club
from apps.game_tracker.models import MatchData
from apps.schedule.models import Season
from apps.team.models.team import Team


def club_seasons(club: Club) -> QuerySet[Season]:
    """Return seasons with a roster or match connected to the club."""
    return (
        Season.objects
        .filter(
            Q(team_data__team__club=club)
            | Q(matches__home_team__club=club)
            | Q(matches__away_team__club=club)
        )
        .distinct()
        .order_by("-start_date")
    )


def club_teams(club: Club, season: Season | None) -> QuerySet[Team]:
    """Return club teams observed in the selected season."""
    queryset = (
        club.teams
        .select_related("club")
        .order_by("name", "id_uuid")
        .fetch_mode(models.FETCH_RAISE)
    )
    if season:
        queryset = queryset.filter(
            Q(team_data__season_id=season.id_uuid)
            | Q(home_matches__season_id=season.id_uuid)
            | Q(away_matches__season_id=season.id_uuid)
        ).distinct()
    return queryset


def club_matches(club: Club, season: Season | None) -> QuerySet[MatchData]:
    """Return tracker match data involving a club."""
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
        .filter(
            Q(match_link__home_team__club=club) | Q(match_link__away_team__club=club),
        )
        .fetch_mode(models.FETCH_RAISE)
    )
    if season:
        queryset = queryset.filter(match_link__season_id=season.id_uuid)
    return queryset
