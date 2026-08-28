"""Typed application boundary for player-group designation."""

from __future__ import annotations

from dataclasses import dataclass

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models import Q
from django.utils import timezone

from apps.game_tracker.application.ports import MatchChangePublisher
from apps.game_tracker.models import MatchData, MatchPlayer, PlayerGroup
from apps.game_tracker.realtime.contracts import LiveResource
from apps.game_tracker.services.live_updates import record_match_change
from apps.game_tracker.services.match_mutations import (
    locked_match_mutation,
    require_match_revision,
)
from apps.game_tracker.services.player_groups import (
    PlayerGroupAssignmentError,
    add_player_to_group,
)
from apps.player.models import Player, PlayerClubMembership
from apps.schedule.models import Match
from apps.team.models import Team, TeamData


MAX_RESERVE_PLAYERS = 16
MAX_STARTING_PLAYERS = 4
PLAYER_GROUP_EDIT_PERMISSION_ERROR = "You do not have permission to edit player groups."

DESIGNATION_RESOURCES = {
    LiveResource.TRACKER,
    LiveResource.PLAYER_GROUPS,
    LiveResource.STATS,
    LiveResource.IMPACTS,
}


@dataclass(frozen=True, slots=True)
class PlayerDesignationSelection:
    """One player and the group the client currently associates with them."""

    player_id: str
    source_group_id: str | None = None


@dataclass(frozen=True, slots=True)
class DesignatePlayersCommand:
    """Move or remove players within one match-team lineup."""

    players: tuple[PlayerDesignationSelection, ...]
    target_group_id: str | None
    expected_revision: int


@dataclass(frozen=True, slots=True)
class PlayerDesignationResult:
    """Committed designation outcome."""

    match_data: MatchData
    revision: int


@dataclass(slots=True)
class PlayerDesignationValidationError(Exception):
    """A designation command contains invalid domain input."""

    message: str

    def __str__(self) -> str:
        """Return the user-facing validation message."""
        return self.message


class PlayerDesignationPermissionError(PermissionError):
    """The actor cannot edit the requested match-team lineup."""


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


def can_edit_player_groups(*, user: object, match: Match, team: Team) -> bool:
    """Return whether the user can edit player groups for the match team."""
    if not getattr(user, "is_authenticated", False):
        return False

    if _is_coach_or_admin_user(user):
        return True

    player = Player.objects.filter(user=user).first()
    if player is None:
        return False

    match_date = timezone.localdate(match.start_time)
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


def _apply_designation(
    *,
    command: DesignatePlayersCommand,
    players_by_id: dict[str, Player],
    groups_by_id: dict[str, PlayerGroup],
    target_group: PlayerGroup | None,
) -> None:
    """Apply already-resolved designation changes."""
    for selection in command.players:
        player = players_by_id[selection.player_id]
        old_group = (
            groups_by_id.get(selection.source_group_id)
            if selection.source_group_id is not None
            else None
        )
        if target_group is not None:
            add_player_to_group(
                player=player,
                target_group=target_group,
                source_group=old_group,
            )
        elif old_group is not None:
            old_group.players.remove(player)


def _validate_target_group_capacity(
    *,
    target_group: PlayerGroup,
    player_ids: set[str],
) -> bool:
    """Return True if adding selected players would not overflow group limits."""
    is_reserve_group = target_group.starting_type.name == "Reserve"
    max_group_players = (
        MAX_RESERVE_PLAYERS if is_reserve_group else MAX_STARTING_PLAYERS
    )

    already_in_target_group = set(
        target_group.players.filter(id_uuid__in=player_ids).values_list(
            "id_uuid",
            flat=True,
        ),
    )

    normalized_existing_ids = {str(player_id) for player_id in already_in_target_group}
    players_to_add_count = len(player_ids - normalized_existing_ids)
    final_group_size = target_group.players.count() + players_to_add_count
    return final_group_size <= max_group_players


def _referenced_group_ids(command: DesignatePlayersCommand) -> set[str]:
    group_ids = {
        selection.source_group_id
        for selection in command.players
        if selection.source_group_id is not None
    }
    if command.target_group_id is not None:
        group_ids.add(command.target_group_id)
    return group_ids


def _load_groups(group_ids: set[str]) -> dict[str, PlayerGroup]:
    try:
        groups = list(
            PlayerGroup.objects.filter(id_uuid__in=group_ids).select_related(
                "match_data__match_link",
                "team__club",
                "starting_type",
            )
        )
    except (DjangoValidationError, ValueError) as exc:
        raise PlayerDesignationValidationError("Invalid player group context") from exc
    return {str(group.id_uuid): group for group in groups}


def _resolve_context(
    command: DesignatePlayersCommand,
) -> tuple[MatchData, Match, Team]:
    group_ids = _referenced_group_ids(command)
    if not group_ids:
        raise PlayerDesignationValidationError("Invalid player group context")

    groups_by_id = _load_groups(group_ids)
    if (
        command.target_group_id is not None
        and command.target_group_id not in groups_by_id
    ):
        raise PlayerDesignationValidationError("Unknown player group")
    if len(groups_by_id) != len(group_ids):
        raise PlayerDesignationValidationError("Invalid player group context")

    match_data_ids = {group.match_data_id for group in groups_by_id.values()}
    team_ids = {group.team_id for group in groups_by_id.values()}
    if len(match_data_ids) != 1 or len(team_ids) != 1:
        raise PlayerDesignationValidationError("Invalid player group context")

    base_group = next(iter(groups_by_id.values()))
    return base_group.match_data, base_group.match_data.match_link, base_group.team


def _load_locked_command_state(
    *,
    command: DesignatePlayersCommand,
    match_data: MatchData,
    team: Team,
) -> tuple[dict[str, PlayerGroup], dict[str, Player]]:
    group_ids = _referenced_group_ids(command)
    groups_by_id = _load_groups(group_ids)
    if (
        len(groups_by_id) != len(group_ids)
        or any(group.match_data_id != match_data.pk for group in groups_by_id.values())
        or any(group.team_id != team.pk for group in groups_by_id.values())
    ):
        raise PlayerDesignationValidationError("Invalid player group context")

    player_ids = {selection.player_id for selection in command.players}
    try:
        players_by_id = {
            str(player.id_uuid): player
            for player in Player.objects.filter(id_uuid__in=player_ids)
        }
    except (DjangoValidationError, ValueError) as exc:
        raise PlayerDesignationValidationError("Invalid player") from exc
    if len(players_by_id) != len(player_ids):
        raise PlayerDesignationValidationError("Invalid player")
    return groups_by_id, players_by_id


def apply_player_designation(
    *,
    actor: object,
    command: DesignatePlayersCommand,
    publisher: MatchChangePublisher,
) -> PlayerDesignationResult:
    """Validate and apply one lineup mutation under the aggregate lock.

    Raises:
        PlayerDesignationPermissionError: If the actor cannot edit the lineup.
        PlayerDesignationValidationError: If the command violates lineup rules.

    """
    match_data, match, team = _resolve_context(command)
    if not can_edit_player_groups(user=actor, match=match, team=team):
        raise PlayerDesignationPermissionError(PLAYER_GROUP_EDIT_PERMISSION_ERROR)

    try:
        with locked_match_mutation(match_data.pk) as locked:
            require_match_revision(
                locked,
                expected_revision=command.expected_revision,
            )
            groups_by_id, players_by_id = _load_locked_command_state(
                command=command,
                match_data=locked,
                team=team,
            )
            target_group = (
                groups_by_id[command.target_group_id]
                if command.target_group_id is not None
                else None
            )
            if target_group is not None and not _validate_target_group_capacity(
                target_group=target_group,
                player_ids=set(players_by_id),
            ):
                raise PlayerDesignationValidationError("Too many players selected")

            _apply_designation(
                command=command,
                players_by_id=players_by_id,
                groups_by_id=groups_by_id,
                target_group=target_group,
            )
            sync_match_players(match_data=locked)
            revision = record_match_change(
                locked,
                resources=DESIGNATION_RESOURCES,
                publisher=publisher,
            )
            return PlayerDesignationResult(match_data=locked, revision=revision)
    except PlayerGroupAssignmentError as exc:
        raise PlayerDesignationValidationError(str(exc)) from exc


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
