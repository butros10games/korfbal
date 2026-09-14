"""Query service for club overview and eligibility screens."""

from __future__ import annotations

from collections import defaultdict

from django.db import models
from django.db.models import Exists, OuterRef, Q, QuerySet

from apps.club.models.club import Club
from apps.competition.models import (
    PoolEntry,
    Team as CompetitionTeam,
)
from apps.game_tracker.models import MatchData
from apps.schedule.models import Match, Season
from apps.team.models.team import Team
from apps.team.models.team_data import TeamData


def club_seasons(club: Club) -> QuerySet[Season]:
    """Return seasons with a roster or match connected to the club."""
    return Season.objects.filter(
        Q(pk__in=TeamData.objects.filter(team__club=club).values("season_id"))
        | Q(pk__in=Match.objects.filter(home_team__club=club).values("season_id"))
        | Q(pk__in=Match.objects.filter(away_team__club=club).values("season_id"))
    ).order_by("-start_date")


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
            Exists(TeamData.objects.filter(team_id=OuterRef("pk"), season=season))
            | Exists(Match.objects.filter(home_team_id=OuterRef("pk"), season=season))
            | Exists(Match.objects.filter(away_team_id=OuterRef("pk"), season=season))
        )
    return queryset


def club_matches(club: Club, season: Season | None) -> QuerySet[MatchData]:
    """Return tracker match data involving a club."""
    # Filter match foreign keys before loading the home/away presentation data.
    # An OR across both joined clubs otherwise performs those joins for every
    # unrelated fixture encountered by the ordered match scan.
    team_ids = Team.objects.filter(club=club).order_by().values("pk")
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
            Q(match_link__home_team_id__in=team_ids)
            | Q(match_link__away_team_id__in=team_ids),
        )
        .fetch_mode(models.FETCH_RAISE)
    )
    if season:
        queryset = queryset.filter(match_link__season_id=season.id_uuid)
    return queryset


def eligibility_classifications(
    team_data_qs: QuerySet[TeamData],
) -> dict[int, set[tuple[str, str, str, str]]]:
    """Read official classifications without mistaking import defaults for B/rank 1."""
    classifications: dict[int, set[tuple[str, str, str, str]]] = defaultdict(set)
    for entry in PoolEntry.objects.filter(
        team__local_team_data__in=team_data_qs,
    ).select_related("team", "pool__competition_class__edition"):
        classification = entry.pool.competition_class
        if classification is None:
            classifications[entry.team.local_team_data_id or 0].add((
                "unknown",
                "unknown",
                "",
                "",
            ))
            continue
        classifications[entry.team.local_team_data_id or 0].add((
            classification.category,
            classification.age_group,
            f"league:{classification.team_kind}"
            if classification.code == "league"
            else str(classification.pk),
            str(classification.edition_id),
        ))

    for team_data_id in CompetitionTeam.objects.filter(
        local_team_data__in=team_data_qs,
    ).values_list("local_team_data_id", flat=True):
        if team_data_id is not None and team_data_id not in classifications:
            classifications[team_data_id].add(("unknown", "unknown", "", ""))
    return classifications
