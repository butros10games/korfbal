"""Module contains the admin classes for the schedule app."""

from .match_admin import MatchAdmin
from .mvp_admin import MatchMvpAdmin, MatchMvpVoteAdmin
from .season_admin import SeasonAdmin


__all__ = [
    "MatchAdmin",
    "MatchMvpAdmin",
    "MatchMvpVoteAdmin",
    "SeasonAdmin",
]
