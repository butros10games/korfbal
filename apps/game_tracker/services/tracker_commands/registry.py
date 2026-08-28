"""Single registry for tracker parsing and execution metadata."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from apps.game_tracker.realtime.contracts import ALL_LIVE_RESOURCES, LiveResource

from .base import TrackerCommand, TrackerCommandError
from .lifecycle import (
    NewAttackCommand,
    PartEndCommand,
    StartPauseCommand,
    TimeoutCommand,
)
from .scoring import GoalCommand, ShotCommand
from .substitutions import (
    GetNonActivePlayersCommand,
    OpponentSubstitutionCommand,
    SubstituteCommand,
)
from .undo import RemoveLastEventCommand


CommandParser = Callable[[dict[str, Any]], TrackerCommand]


@dataclass(frozen=True, slots=True)
class CommandDefinition:
    """Parsing and execution metadata for one public tracker command."""

    name: str
    parse: CommandParser
    resources: frozenset[LiveResource] = frozenset()
    mutating: bool = True
    server_timed: bool = False


def _constant(command: TrackerCommand) -> CommandParser:
    return lambda _payload: command


def _parse_timeout(payload: dict[str, Any]) -> TrackerCommand:
    for_team = payload.get("for_team")
    if not isinstance(for_team, bool):
        raise TrackerCommandError("Invalid timeout payload.", code="bad_request")
    return TimeoutCommand(for_team=for_team)


def _parse_shot(payload: dict[str, Any]) -> TrackerCommand:
    player_id = payload.get("player_id")
    for_team = payload.get("for_team")
    shot_type = payload.get("shot_type")
    if shot_type is None:
        shot_type = payload.get("goal_type")

    if not isinstance(player_id, str) or not isinstance(for_team, bool):
        raise TrackerCommandError("Invalid shot_reg payload.", code="bad_request")
    if shot_type is not None and not isinstance(shot_type, str):
        raise TrackerCommandError("Invalid shot type.", code="bad_request")
    return ShotCommand(
        player_id=player_id,
        for_team=for_team,
        shot_type_id=shot_type,
    )


def _parse_goal(payload: dict[str, Any]) -> TrackerCommand:
    player_id = payload.get("player_id")
    goal_type = payload.get("goal_type")
    for_team = payload.get("for_team")
    if (
        not isinstance(player_id, str)
        or not isinstance(goal_type, str)
        or not isinstance(for_team, bool)
    ):
        raise TrackerCommandError("Invalid goal_reg payload.", code="bad_request")
    return GoalCommand(
        player_id=player_id,
        goal_type_id=goal_type,
        for_team=for_team,
    )


def _parse_substitution(payload: dict[str, Any]) -> TrackerCommand:
    new_player_id = payload.get("new_player_id")
    old_player_id = payload.get("old_player_id")
    if not isinstance(new_player_id, str) or not isinstance(old_player_id, str):
        raise TrackerCommandError("Invalid substitute_reg payload.", code="bad_request")
    return SubstituteCommand(
        new_player_id=new_player_id,
        old_player_id=old_player_id,
    )


COMMAND_DEFINITIONS = (
    CommandDefinition(
        name="start/pause",
        parse=_constant(StartPauseCommand()),
        resources=frozenset({
            LiveResource.LIVE,
            LiveResource.TRACKER,
            LiveResource.EVENTS,
        }),
        server_timed=True,
    ),
    CommandDefinition(
        name="part_end",
        parse=_constant(PartEndCommand()),
        resources=frozenset(ALL_LIVE_RESOURCES),
        server_timed=True,
    ),
    CommandDefinition(
        name="timeout",
        parse=_parse_timeout,
        resources=frozenset({
            LiveResource.LIVE,
            LiveResource.TRACKER,
            LiveResource.EVENTS,
        }),
        server_timed=True,
    ),
    CommandDefinition(
        name="new_attack",
        parse=_constant(NewAttackCommand()),
        resources=frozenset({LiveResource.TRACKER, LiveResource.EVENTS}),
    ),
    CommandDefinition(
        name="shot_reg",
        parse=_parse_shot,
        resources=frozenset({
            LiveResource.TRACKER,
            LiveResource.EVENTS,
            LiveResource.SHOTS,
            LiveResource.STATS,
            LiveResource.IMPACTS,
        }),
    ),
    CommandDefinition(
        name="goal_reg",
        parse=_parse_goal,
        resources=frozenset({
            LiveResource.LIVE,
            LiveResource.TRACKER,
            LiveResource.SUMMARY,
            LiveResource.EVENTS,
            LiveResource.SHOTS,
            LiveResource.STATS,
            LiveResource.IMPACTS,
            LiveResource.MVP,
        }),
    ),
    CommandDefinition(
        name="get_non_active_players",
        parse=_constant(GetNonActivePlayersCommand()),
        mutating=False,
    ),
    CommandDefinition(
        name="substitute_reg",
        parse=_parse_substitution,
        resources=frozenset({
            LiveResource.TRACKER,
            LiveResource.EVENTS,
            LiveResource.PLAYER_GROUPS,
            LiveResource.STATS,
            LiveResource.IMPACTS,
        }),
    ),
    CommandDefinition(
        name="substitute_against_reg",
        parse=_constant(OpponentSubstitutionCommand()),
        resources=frozenset({
            LiveResource.TRACKER,
            LiveResource.EVENTS,
            LiveResource.STATS,
            LiveResource.IMPACTS,
        }),
    ),
    CommandDefinition(
        name="remove_last_event",
        parse=_constant(RemoveLastEventCommand()),
        resources=frozenset(ALL_LIVE_RESOURCES),
    ),
)
_COMMANDS_BY_NAME = {definition.name: definition for definition in COMMAND_DEFINITIONS}


def command_definition(payload: dict[str, Any]) -> CommandDefinition:
    """Resolve and validate the definition for an incoming command payload.

    Raises:
        TrackerCommandError: If the command name is missing or unknown.

    """
    command = payload.get("command")
    if not isinstance(command, str):
        raise TrackerCommandError("Missing command.", code="bad_request")
    definition = _COMMANDS_BY_NAME.get(command)
    if definition is None:
        raise TrackerCommandError(f"Unknown command: {command}", code="bad_request")
    return definition
