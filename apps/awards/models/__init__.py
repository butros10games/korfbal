"""Models for awards.

We keep MVP models here, backed by the existing DB tables created historically
by the `schedule` app migrations.
"""

from .mvp import MatchMvp, MatchMvpVote


__all__ = [
    "MatchMvp",
    "MatchMvpVote",
]
