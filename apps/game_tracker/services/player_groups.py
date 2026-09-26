"""Helpers for keeping match player groups consistent."""

from __future__ import annotations

from dataclasses import dataclass

from django.db.models import Exists, OuterRef, Q, QuerySet
from django.utils import timezone

from apps.game_tracker.models import (
    GroupType,
    MatchData,
    MatchGuestPlayer,
    PlayerGroup,
)
from apps.player.models import Player, PlayerClubMembership
from apps.schedule.models import Match
from apps.team.models import Team, TeamData


RESERVE_GROUP_NAME = "Reserve"


@dataclass(slots=True)
class PlayerGroupAssignmentError(ValueError):
    """Raised when a player-group mutation would break tracker rules."""

    message: str

    def __str__(self) -> str:
        """Return the user-facing error string."""
        return self.message


def club_lineup_players(*, match: Match, team: Team) -> QuerySet[Player]:
    """Return the club's eligible picker candidates for this match's date/season.

    Guests added for this match and team stay eligible so a removed guest can
    be selected again; they never become candidates for any other match.
    """
    match_date = timezone.localdate(match.start_time)
    season_rosters = TeamData.objects.filter(
        team__club_id=team.club_id,
        season_id=match.season_id,
    )
    memberships = PlayerClubMembership.objects.filter(
        player_id=OuterRef("pk"),
        club_id=team.club_id,
        start_date__lte=match_date,
    ).filter(Q(end_date__isnull=True) | Q(end_date__gte=match_date))
    return Player.objects.filter(
        Exists(season_rosters.filter(players=OuterRef("pk")))
        | Exists(season_rosters.filter(coach=OuterRef("pk")))
        | Exists(memberships)
        | Exists(match_guests_for(match=match, team=team).filter(player=OuterRef("pk")))
    )


def match_guests_for(*, match: Match, team: Team) -> QuerySet[MatchGuestPlayer]:
    """Return the guest links for one match-team selection."""
    return MatchGuestPlayer.objects.filter(match_data__match_link=match, team=team)


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
    """Move one actual member, keeping each player in one group per match.

    Raises:
        PlayerGroupAssignmentError: The source is false, another assignment would
            remain, or a court move does not come from the reserve group.

    """
    current_group_ids = set(
        PlayerGroup.objects.filter(
            match_data=target_group.match_data, players=player
        ).values_list("pk", flat=True)
    )
    if source_group is not None and source_group.pk not in current_group_ids:
        raise PlayerGroupAssignmentError("Player is not in the selected source group")

    effective_source_group = source_group
    if target_group.starting_type.name != RESERVE_GROUP_NAME:
        reserve_group = get_reserve_group(
            match_data=target_group.match_data,
            team=target_group.team,
        )
        if effective_source_group is None and reserve_group.pk in current_group_ids:
            effective_source_group = reserve_group
        if (
            effective_source_group is None
            or effective_source_group.pk != reserve_group.pk
        ):
            raise PlayerGroupAssignmentError(
                f"{player} is not in the reserve player group.",
            )

    allowed_group_ids = {target_group.pk}
    if effective_source_group is not None:
        allowed_group_ids.add(effective_source_group.pk)
    if current_group_ids - allowed_group_ids:
        raise PlayerGroupAssignmentError("Player is already in another player group")
    if (
        effective_source_group is not None
        and effective_source_group.pk != target_group.pk
    ):
        effective_source_group.players.remove(player)
    target_group.players.add(player)
