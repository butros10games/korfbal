"""This module contains signals for the game_tracker app."""

from .match_data_signals import create_player_groups_for_new_match_data
from .match_signals import create_match_data_for_new_match


__all__ = [
    "create_match_data_for_new_match",
    "create_player_groups_for_new_match_data",
]
