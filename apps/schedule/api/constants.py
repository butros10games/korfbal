"""Shared constants for the schedule API.

Keep these in a dedicated module to avoid circular imports when splitting large
view modules into mixins.
"""

from __future__ import annotations


MATCH_TRACKER_DATA_NOT_FOUND = "Match tracker data not found."

MVP_VOTE_COOKIE_NAME = "korfbal_mvp_vote_tokens"
MVP_VOTE_COOKIE_SALT = "korfbal.schedule.mvp.vote_tokens.v1"
MVP_VOTE_COOKIE_MAX_AGE_SECONDS = 60 * 60 * 24 * 30
