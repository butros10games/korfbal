"""Module contains the consumers for the game_tracker app."""

from .match_data import MatchDataConsumer
from .match_tracker import MatchTrackerConsumer


__all__ = ["MatchDataConsumer", "MatchTrackerConsumer"]
