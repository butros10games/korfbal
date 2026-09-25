"""Add club-less guest players to one match-team selection."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from apps.game_tracker.application.ports import MatchChangePublisher
from apps.game_tracker.models import MatchData, MatchGuestPlayer, PlayerGroup
from apps.game_tracker.services.live_updates import record_match_change
from apps.game_tracker.services.match_mutations import (
    locked_match_mutation,
    require_match_revision,
)
from apps.game_tracker.services.player_designation import (
    DESIGNATION_RESOURCES,
    MAX_RESERVE_PLAYERS,
    PLAYER_GROUP_EDIT_PERMISSION_ERROR,
    PlayerDesignationPermissionError,
    PlayerDesignationValidationError,
    can_edit_player_groups,
    sync_match_players_for_team,
)
from apps.game_tracker.services.player_groups import RESERVE_GROUP_NAME
from apps.game_tracker.services.tracker_access import has_tracker_grant
from apps.player.models import Player
from apps.schedule.models import Match
from apps.team.models import Team


MIN_GUEST_NAME_LENGTH = 2
MAX_GUEST_NAME_LENGTH = 80


@dataclass(frozen=True, slots=True)
class AddGuestPlayerCommand:
    """Create a named guest and place them in the team's reserve group."""

    match_id: str
    team_id: str
    name: str
    expected_revision: int


@dataclass(frozen=True, slots=True)
class AddGuestPlayerResult:
    """Committed guest-player outcome."""

    player: Player
    revision: int


def normalize_guest_name(value: str) -> str:
    """Collapse whitespace and validate the guest's display name.

    Raises:
        PlayerDesignationValidationError: The name is too short or too long.

    """
    name = " ".join(value.split())
    if len(name) < MIN_GUEST_NAME_LENGTH:
        raise PlayerDesignationValidationError(
            f"Guest name should be at least {MIN_GUEST_NAME_LENGTH} characters long"
        )
    if len(name) > MAX_GUEST_NAME_LENGTH:
        raise PlayerDesignationValidationError(
            f"Guest name should be at most {MAX_GUEST_NAME_LENGTH} characters long"
        )
    return name


def _resolve_scope(command: AddGuestPlayerCommand) -> tuple[Match, Team]:
    match = (
        Match.objects
        .select_related("home_team__club", "away_team__club")
        .filter(pk=command.match_id)
        .first()
    )
    if match is None:
        raise PlayerDesignationValidationError("Invalid player group context")
    for team in (match.home_team, match.away_team):
        if str(team.pk) == command.team_id:
            return match, team
    raise PlayerDesignationValidationError("Invalid player group context")


def add_guest_player(
    *,
    actor: object,
    command: AddGuestPlayerCommand,
    publisher: MatchChangePublisher,
    tracker_grants: Mapping[str, str] | None = None,
) -> AddGuestPlayerResult:
    """Create a guest player for this match only and add them to the reserve.

    Raises:
        PlayerDesignationPermissionError: If the actor cannot edit the lineup.
        PlayerDesignationValidationError: If the name or lineup state is invalid.

    """
    name = normalize_guest_name(command.name)
    match, team = _resolve_scope(command)
    if not can_edit_player_groups(user=actor, match=match, team=team) and not (
        tracker_grants and has_tracker_grant(tracker_grants, match=match, team=team)
    ):
        raise PlayerDesignationPermissionError(PLAYER_GROUP_EDIT_PERMISSION_ERROR)

    match_data = MatchData.objects.filter(match_link=match).first()
    if match_data is None:
        raise PlayerDesignationValidationError("Invalid player group context")

    with locked_match_mutation(match_data.pk) as locked:
        require_match_revision(locked, expected_revision=command.expected_revision)
        reserve_group = PlayerGroup.objects.filter(
            match_data=locked,
            team=team,
            starting_type__name=RESERVE_GROUP_NAME,
        ).first()
        if reserve_group is None:
            raise PlayerDesignationValidationError("Unknown player group")
        if reserve_group.players.count() >= MAX_RESERVE_PLAYERS:
            raise PlayerDesignationValidationError("Too many players selected")

        player = Player.objects.create(name=name)
        MatchGuestPlayer.objects.create(match_data=locked, team=team, player=player)
        reserve_group.players.add(player)
        sync_match_players_for_team(match_data=locked, team=team)
        revision = record_match_change(
            locked,
            resources=DESIGNATION_RESOURCES,
            publisher=publisher,
        )
        return AddGuestPlayerResult(player=player, revision=revision)
