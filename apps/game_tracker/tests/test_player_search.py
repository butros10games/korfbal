"""Tests for match-player name matching."""

from apps.game_tracker.services.player_search import (
    normalize_player_name,
    player_name_match_score,
)


def test_normalize_player_name_removes_accents_and_punctuation() -> None:
    """Search normalization should make common name variants equivalent."""
    assert normalize_player_name("  Joëlle.van-Dĳk  ") == "joelle van dijk"


def test_player_name_match_score_matches_multiple_name_parts() -> None:
    """Separate query terms should match across first and last names."""
    assert (
        player_name_match_score(
            "joelle dijk",
            username="joelle_17",
            first_name="Joëlle",
            last_name="van Dijk",
        )
        == 1
    )


def test_player_name_match_score_rejects_short_loose_matches() -> None:
    """Short terms should require an exact substring to avoid noisy results."""
    assert (
        player_name_match_score(
            "jan",
            username="joan",
            first_name="",
            last_name="",
        )
        is None
    )
