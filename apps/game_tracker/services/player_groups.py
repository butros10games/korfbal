"""Helpers for keeping match player groups consistent."""

from __future__ import annotations

from dataclasses import dataclass

from apps.game_tracker.models import GroupType, MatchData, PlayerGroup
from apps.player.models import Player
from apps.team.models import Team


RESERVE_GROUP_NAME = "Reserve"


@dataclass(frozen=True, slots=True)
class PlayerGroupAssignmentError(ValueError):
    """Raised when a player-group mutation would break tracker rules."""

    message: str

    def __str__(self) -> str:
        """Return the user-facing error string."""
        return self.message


def ensure_player_groups_for_match_data(match_data: MatchData) -> None:
    """Create any missing PlayerGroup rows for both teams in a match."""
    group_types = list(GroupType.objects.order_by("order", "name"))
    if not group_types:
        return

    match_link = match_data.match_link
    teams = (match_link.home_team, match_link.away_team)
    existing_group_keys = set(
        PlayerGroup.objects.filter(match_data=match_data, team__in=teams).values_list(
            "team_id", "starting_type_id"
        )
    )

    missing_groups = [
        PlayerGroup(
            match_data=match_data,
            team=team,
            starting_type=group_type,
            current_type=group_type,
        )
        for team in teams
        for group_type in group_types
        if (team.id_uuid, group_type.id_uuid) not in existing_group_keys
    ]
    if missing_groups:
        PlayerGroup.objects.bulk_create(missing_groups)


def ensure_player_groups_for_group_type(group_type: GroupType) -> None:
    """Backfill PlayerGroup rows for a newly created group type."""
    del group_type
    for match_data in MatchData.objects.select_related(
        "match_link__home_team",
        "match_link__away_team",
    ):
        ensure_player_groups_for_match_data(match_data)


def get_reserve_group(*, match_data: MatchData, team: Team) -> PlayerGroup:
    """Return the team's reserve group for a match."""
    return PlayerGroup.objects.get(
        team=team,
        match_data=match_data,
        starting_type__name=RESERVE_GROUP_NAME,
    )


def add_player_to_group(
    *,
    player: Player,
    target_group: PlayerGroup,
    source_group: PlayerGroup | None = None,
) -> None:
    """Add a player to a group, enforcing the reserve-group boundary.

    Raises:
        PlayerGroupAssignmentError: If the player is assigned to a non-reserve
            group without coming from the reserve group.

    """
    if target_group.starting_type.name == RESERVE_GROUP_NAME:
        if source_group is not None and source_group.id_uuid != target_group.id_uuid:
            source_group.players.remove(player)
        target_group.players.add(player)
        return

    reserve_group = get_reserve_group(
        match_data=target_group.match_data,
        team=target_group.team,
    )
    effective_source_group = source_group
    if (
        effective_source_group is None
        and reserve_group.players.filter(
            id_uuid=player.id_uuid,
        ).exists()
    ):
        effective_source_group = reserve_group

    if (
        effective_source_group is None
        or effective_source_group.id_uuid != reserve_group.id_uuid
    ):
        raise PlayerGroupAssignmentError(
            f"{player} is not in the reserve player group.",
        )

    if source_group is not None and source_group.id_uuid != reserve_group.id_uuid:
        source_group.players.remove(player)
    reserve_group.players.remove(player)
    target_group.players.add(player)
