"""Module contains the admin classes for the game_tracker app."""

from .attack_admin import AttackAdmin
from .goal_type_admin import GoalTypeAdmin
from .group_type_admin import GroupTypeAdmin
from .match_data_admin import MatchDataAdmin
from .match_part_admin import MatchPartAdmin
from .match_player_admin import MatchPlayerAdmin
from .pause_admin import PauseAdmin
from .player_change_admin import PlayerChangeAdmin
from .player_group_admin import PlayerGroupAdmin
from .player_match_impact_admin import PlayerMatchImpactAdmin
from .player_match_impact_breakdown_admin import PlayerMatchImpactBreakdownAdmin
from .shot_admin import ShotAdmin
from .timeout_admin import TimeoutAdmin


__all__ = [
    "AttackAdmin",
    "GoalTypeAdmin",
    "GroupTypeAdmin",
    "MatchDataAdmin",
    "MatchPartAdmin",
    "MatchPlayerAdmin",
    "PauseAdmin",
    "PlayerChangeAdmin",
    "PlayerGroupAdmin",
    "PlayerMatchImpactAdmin",
    "PlayerMatchImpactBreakdownAdmin",
    "ShotAdmin",
    "TimeoutAdmin",
]
