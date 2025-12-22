"""Compatibility re-exports for MVP service helpers.

MVP now lives in `apps.awards.services.mvp`.

This module remains as a thin shim so existing imports keep working.
"""

from apps.awards.services.mvp import (
    MvpCandidate,
    build_mvp_candidates,
    cast_vote,
    cast_vote_anon,
    ensure_mvp_published,
    get_or_create_match_mvp,
)


__all__ = [
    "MvpCandidate",
    "build_mvp_candidates",
    "cast_vote",
    "cast_vote_anon",
    "ensure_mvp_published",
    "get_or_create_match_mvp",
]
