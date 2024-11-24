"""This module contains the admin classes for the game_tracker app."""

from .goal_type_admin import GoalTypeAdmin
from .group_type_admin import GroupTypeAdmin
from .match_data_admin import MatchDataAdmin
from .match_part_admin import MatchPartAdmin
from .match_player_admin import MatchPlayerAdmin
from .pause_admin import PauseAdmin
from .player_change_admin import PlayerChangeAdmin
from .player_group_admin import PlayerGroupAdmin
from .shot_admin import ShotAdmin

__all__ = [
    "GoalTypeAdmin",
    "GroupTypeAdmin",
    "MatchDataAdmin",
    "MatchPartAdmin",
    "MatchPlayerAdmin",
    "PauseAdmin",
    "PlayerChangeAdmin",
    "PlayerGroupAdmin",
    "ShotAdmin",
]
