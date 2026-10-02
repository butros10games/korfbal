"""Query helpers for player-followed and player-team API endpoints."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from django.db.models import Q, QuerySet
from django.utils import timezone

from apps.player.models.player import Player
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


def running_rosters() -> QuerySet[TeamData]:
    """Return team seasons running today, for filtering by the player's role.

    Indoor and outdoor competitions overlap, so a player can play in several
    seasons at once. This is resolved from the player's rosters, never from a
    catalogue-wide "current season" chosen by unrelated fixtures.
    """
    today = timezone.localdate()
    return TeamData.objects.filter(
        season__start_date__lte=today, season__end_date__gte=today
    )


def connected_team_ids(player: Player) -> list[UUID]:
    """Return followed teams plus running teams the player plays in or coaches.

    Players are often placed on a roster without following that team, so
    "followed" views must also include their own teams.
    """
    team_ids = set(player.team_follow.values_list("id_uuid", flat=True))
    team_ids.update(
        running_rosters()
        .filter(Q(players=player) | Q(coach=player))
        .values_list("team_id", flat=True)
    )
    return sorted(team_ids)


def grouped_teams_for_player(player: Player) -> PlayerTeamCollections:
    """Return running playing/coaching teams and followed teams separately."""
    playing_ids = (
        running_rosters()
        .filter(players=player)
        .values_list("team_id", flat=True)
        .distinct()
    )
    coaching_ids = (
        running_rosters()
        .filter(coach=player)
        .values_list("team_id", flat=True)
        .distinct()
    )
    return PlayerTeamCollections(
        playing=Team.objects
        .filter(id_uuid__in=playing_ids)
        .select_related("club")
        .order_by("club__name", "name"),
        coaching=Team.objects
        .filter(id_uuid__in=coaching_ids)
        .select_related("club")
        .order_by("club__name", "name"),
        following=followed_teams_for_player(player),
    )
