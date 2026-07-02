"""Player designation workflow services."""

from __future__ import annotations

from typing import Any

from django.db.models import Q

from apps.game_tracker.models import MatchData, MatchPlayer, PlayerGroup
from apps.game_tracker.services.player_groups import (
    PlayerGroupAssignmentError,
    add_player_to_group,
)
from apps.player.models import Player, PlayerClubMembership
from apps.schedule.models import Match
from apps.team.models import Team, TeamData


MAX_RESERVE_PLAYERS = 16
MAX_STARTING_PLAYERS = 4


def sync_match_players_for_team(*, match_data: MatchData, team: Team) -> None:
    """Sync MatchPlayer rows from current PlayerGroup assignments."""
    desired_ids = set(
        Player.objects
        .filter(
            player_groups__match_data=match_data,
            player_groups__team=team,
        )
        .values_list("id_uuid", flat=True)
        .distinct()
    )

    existing_ids = set(
        MatchPlayer.objects
        .filter(match_data=match_data, team=team)
        .values_list("player_id", flat=True)
        .distinct()
    )

    to_create = desired_ids - existing_ids
    to_delete = existing_ids - desired_ids

    if to_create:
        MatchPlayer.objects.bulk_create(
            [
                MatchPlayer(match_data=match_data, team=team, player_id=player_id)
                for player_id in to_create
            ],
            ignore_conflicts=True,
        )

    if to_delete:
        MatchPlayer.objects.filter(
            match_data=match_data,
            team=team,
            player_id__in=list(to_delete),
        ).delete()


def sync_match_players(*, match_data: MatchData) -> None:
    """Sync MatchPlayer rows for both teams in the match."""
    match = match_data.match_link
    sync_match_players_for_team(match_data=match_data, team=match.home_team)
    sync_match_players_for_team(match_data=match_data, team=match.away_team)


def get_player_group(group_id: object) -> PlayerGroup | None:
    """Return a player group by id when the payload value is usable."""
    if not isinstance(group_id, str) or not group_id:
        return None
    return PlayerGroup.objects.filter(id_uuid=group_id).first()


def resolve_designation_context(
    *,
    selected_players: list[dict[str, Any]],
    target_group: PlayerGroup | None,
) -> tuple[Match | None, Team | None]:
    """Resolve the match/team context referenced by designation payload."""
    group_ids: set[str] = set()
    if target_group is not None:
        group_ids.add(str(target_group.id_uuid))

    for player_data in selected_players:
        group_id = player_data.get("groupId")
        if isinstance(group_id, str) and group_id:
            group_ids.add(group_id)

    if not group_ids:
        return None, None

    groups = list(
        PlayerGroup.objects.filter(id_uuid__in=group_ids).select_related(
            "match_data__match_link",
            "team__club",
        ),
    )
    if not groups or len(groups) != len(group_ids):
        return None, None

    if len({group.match_data_id for group in groups}) != 1:
        return None, None
    if len({group.team_id for group in groups}) != 1:
        return None, None

    base_group = groups[0]
    return base_group.match_data.match_link, base_group.team


def can_edit_player_groups(*, user: object, match: Match, team: Team) -> bool:
    """Return whether the user can edit player groups for the match team."""
    if not getattr(user, "is_authenticated", False):
        return False

    if _is_coach_or_admin_user(user):
        return True

    player = Player.objects.filter(user=user).first()
    if player is None:
        return False

    match_date = match.start_time.date()
    membership_allowed = (
        PlayerClubMembership.objects
        .filter(player=player, club=team.club, start_date__lte=match_date)
        .filter(Q(end_date__isnull=True) | Q(end_date__gte=match_date))
        .exists()
    )
    if membership_allowed:
        return True

    return (
        TeamData.objects
        .filter(team__club=team.club, season=match.season)
        .filter(Q(players=player) | Q(coach=player))
        .exists()
    )


def apply_designation(
    *,
    selected_players: list[dict[str, Any]],
    target_group: PlayerGroup | None,
) -> tuple[PlayerGroup | None, str | None]:
    """Apply designation changes and return a group usable for sync."""
    resolved_group = target_group

    for player_data in selected_players:
        player_id = player_data.get("id_uuid")
        if not player_id:
            continue

        player = Player.objects.get(id_uuid=player_id)
        old_group = get_player_group(player_data.get("groupId"))
        if target_group is not None:
            try:
                add_player_to_group(
                    player=player,
                    target_group=target_group,
                    source_group=old_group,
                )
            except PlayerGroupAssignmentError as exc:
                return resolved_group, str(exc)
        elif old_group is not None:
            old_group.players.remove(player)

        if old_group is not None and resolved_group is None:
            resolved_group = old_group

    return resolved_group, None


def validate_target_group_capacity(
    *,
    player_group_model: PlayerGroup,
    selected_players: list[dict[str, Any]],
) -> bool:
    """Return True if adding selected players would not overflow group limits."""
    is_reserve_group = player_group_model.starting_type.name == "Reserve"
    max_group_players = (
        MAX_RESERVE_PLAYERS if is_reserve_group else MAX_STARTING_PLAYERS
    )

    selected_ids = {
        player_data.get("id_uuid")
        for player_data in selected_players
        if player_data.get("id_uuid")
    }

    already_in_target_group = set(
        player_group_model.players.filter(id_uuid__in=selected_ids).values_list(
            "id_uuid",
            flat=True,
        ),
    )

    players_to_add_count = len(selected_ids - already_in_target_group)
    final_group_size = player_group_model.players.count() + players_to_add_count
    return final_group_size <= max_group_players


def _is_coach_or_admin_user(user: object) -> bool:
    if not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "is_staff", False) or getattr(user, "is_superuser", False):
        return True

    groups = getattr(user, "groups", None)
    if groups is None:
        return False
    try:
        return bool(groups.filter(name__iexact="coach").exists())
    except AttributeError:
        return False
