"""Module contains signals for the game_tracker app."""

from .impact_recompute_signals import (  # noqa: F401
    _pause_changed,
    _player_change_changed,
    _player_group_changed,
    _player_group_players_changed,
    _shot_changed,
)
from .match_data_signals import create_player_groups_for_new_match_data
from .match_signals import create_match_data_for_new_match
from .minutes_recompute_signals import (  # noqa: F401
    _match_data_post_save as _minutes_match_data_post_save,
    _match_data_pre_save as _minutes_match_data_pre_save,
    _pause_changed as _minutes_pause_changed,
    _player_change_changed as _minutes_player_change_changed,
    _player_group_changed as _minutes_player_group_changed,
    _player_group_players_changed as _minutes_player_group_players_changed,
    _shot_changed as _minutes_shot_changed,
)


__all__ = [
    "create_match_data_for_new_match",
    "create_player_groups_for_new_match_data",
]
