"""Regression coverage for the tracker command registry contract."""

from __future__ import annotations

from apps.game_tracker.services.tracker_commands.registry import COMMAND_DEFINITIONS


def test_registry_owns_the_complete_command_contract() -> None:
    """Each public command has one self-consistent registry definition."""
    names = [definition.name for definition in COMMAND_DEFINITIONS]
    assert len(set(names)) == len(names)
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
