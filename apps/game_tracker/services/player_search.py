"""Name matching helpers for the match-player picker."""

from __future__ import annotations

from difflib import SequenceMatcher
import unicodedata


MIN_FUZZY_TERM_LENGTH = 4
FUZZY_MATCH_THRESHOLD = 0.8


def normalize_player_name(value: str) -> str:
    """Normalize names for case-, accent-, and punctuation-insensitive matching."""
    decomposed = unicodedata.normalize("NFKD", value.casefold())
    without_marks = "".join(
        character for character in decomposed if not unicodedata.combining(character)
    )
    words = "".join(
        character if character.isalnum() else " " for character in without_marks
    )
    return " ".join(words.split())


def player_name_match_score(
    query: str,
    *,
    username: str,
    first_name: str,
    last_name: str,
) -> int | None:
    """Return a lower-is-better match score, or ``None`` when there is no match."""
    normalized_query = normalize_player_name(query)
    if not normalized_query:
        return None

    normalized_fields = tuple(
        filter(
            None,
            (
                normalize_player_name(username),
                normalize_player_name(first_name),
                normalize_player_name(last_name),
                normalize_player_name(f"{first_name} {last_name}"),
            ),
        ),
    )
    if any(normalized_query in field for field in normalized_fields):
        return 0

    query_terms = normalized_query.split()
    candidate_terms = " ".join(normalized_fields).split()
    if all(
        any(query_term in candidate_term for candidate_term in candidate_terms)
        for query_term in query_terms
    ):
        return 1

    if all(
        any(
            _is_close_term_match(query_term, candidate_term)
            for candidate_term in candidate_terms
        )
        for query_term in query_terms
    ):
        return 2

    return None


def _is_close_term_match(query_term: str, candidate_term: str) -> bool:
    if (
        len(query_term) < MIN_FUZZY_TERM_LENGTH
        or len(candidate_term) < MIN_FUZZY_TERM_LENGTH
    ):
        return False

    maximum_length_difference = max(1, len(query_term) // 4)
    if abs(len(query_term) - len(candidate_term)) > maximum_length_difference:
        return False

    return (
        SequenceMatcher(None, query_term, candidate_term).ratio()
        >= FUZZY_MATCH_THRESHOLD
    )
