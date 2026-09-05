"""Incremental team membership commands; match history is independent."""

from django.db import transaction

from apps.player.models import Player
from apps.schedule.models import Season
from apps.team.models import Team, TeamData


@transaction.atomic
def change_team_membership(
    *, team: Team, season: Season, player: Player, operation: str
) -> None:
    """Serialize membership changes, including teams without a season roster yet."""
    Team.objects.select_for_update().get(pk=team.pk)
    rosters = list(
        TeamData.objects
        .select_for_update()
        .filter(team=team, season=season)
        .order_by("pk")
    )
    if operation == "add":
        roster = (
            rosters[0] if rosters else TeamData.objects.create(team=team, season=season)
        )
        roster.players.add(player)
    else:
        # Legacy data can contain several rows for the same team and season.
        for roster in rosters:
            roster.players.remove(player)
