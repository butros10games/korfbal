"""This module contains the views for the game_tracker app."""

from .match_detail import match_detail
from .match_team_selector import match_team_selector
from .match_tracker import match_tracker
from .player_selection import (
    player_designation,
    player_overview,
    player_overview_data,
    player_search,
    players_team,
)

__all__ = [
    "match_detail",
    "match_team_selector",
    "match_tracker",
    "player_overview",
    "player_search",
    "player_designation",
    "player_overview_data",
    "players_team",
]
