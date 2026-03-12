"""Query helpers for player-followed and player-team API endpoints."""

from __future__ import annotations

from dataclasses import dataclass

from django.db.models import QuerySet

from apps.player.models.player import Player
from apps.player.services.player_overview import current_season
from apps.team.models import TeamData
from apps.team.models.team import Team


@dataclass(frozen=True)
class PlayerTeamCollections:
    """Grouped team querysets for a player."""

    playing: QuerySet[Team]
    coaching: QuerySet[Team]
    following: QuerySet[Team]


def followed_teams_for_player(player: Player) -> QuerySet[Team]:
    """Return followed teams in stable display order."""
    return (
        player.team_follow.all().select_related("club").order_by("club__name", "name")
    )


def grouped_teams_for_player(player: Player) -> PlayerTeamCollections:
    """Return current-season playing/coaching/following team querysets."""
    season = current_season()
    playing_qs = Team.objects.none()
    coaching_qs = Team.objects.none()

    if season is not None:
        playing_ids = (
            TeamData.objects
            .filter(season=season)
            .filter(players=player)
            .values_list("team_id", flat=True)
            .distinct()
        )
        coaching_ids = (
            TeamData.objects
            .filter(season=season)
            .filter(coach=player)
            .values_list("team_id", flat=True)
            .distinct()
        )

        playing_qs = (
            Team.objects
            .filter(id_uuid__in=playing_ids)
            .select_related("club")
            .order_by("club__name", "name")
        )
        coaching_qs = (
            Team.objects
            .filter(id_uuid__in=coaching_ids)
            .select_related("club")
            .order_by("club__name", "name")
        )

    return PlayerTeamCollections(
        playing=playing_qs,
        coaching=coaching_qs,
        following=followed_teams_for_player(player),
    )
