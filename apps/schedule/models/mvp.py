"""Compatibility re-exports for MVP models.

MVP now lives in `apps.awards`.

This module intentionally does *not* define any Django models anymore. It only
re-exports the awards models so legacy imports keep working:

- `from apps.schedule.models.mvp import MatchMvp`
"""

from apps.awards.models.mvp import MatchMvp, MatchMvpVote


__all__ = [
    "MatchMvp",
    "MatchMvpVote",
]
