"""Regression coverage for the tracker command registry contract."""

from __future__ import annotations

from apps.game_tracker.services.tracker_commands.registry import COMMAND_DEFINITIONS


EXPECTED_COMMANDS = {
    "get_non_active_players",
    "goal_reg",
    "new_attack",
    "part_end",
    "remove_last_event",
    "shot_reg",
    "start/pause",
    "substitute_against_reg",
    "substitute_reg",
    "timeout",
}


def test_registry_owns_the_complete_command_contract() -> None:
    """Each public command has one self-consistent registry definition."""
    definitions_by_name = {
        definition.name: definition for definition in COMMAND_DEFINITIONS
    }
    assert set(definitions_by_name) == EXPECTED_COMMANDS
    assert len(definitions_by_name) == len(COMMAND_DEFINITIONS)
    assert {
        definition.name for definition in COMMAND_DEFINITIONS if definition.server_timed
    } == {"part_end", "start/pause", "timeout"}
    assert {
        definition.name for definition in COMMAND_DEFINITIONS if not definition.mutating
    } == {"get_non_active_players"}
    assert all(
        definition.resources
        for definition in COMMAND_DEFINITIONS
        if definition.mutating
    )
