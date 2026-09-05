"""Query-budget and score-source contracts for shared match summaries."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import timedelta

import pytest

from apps.game_tracker.models import MatchData, Shot
from apps.game_tracker.tests.tracker_test_helpers import (
    create_tracker_match,
    create_tracker_player,
)
from apps.kwt_common.utils.match_summary import build_match_summaries


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("status", "goal_counts", "expected_score", "expected_queries"),
    [
        ("upcoming", (1, 1), (7, 4), 0),
        ("finished", (1, 1), (7, 4), 0),
        ("active", (3, 2), (3, 2), 1),
    ],
)
def test_build_match_summaries_selects_score_source_without_per_match_queries(
    status: str,
    goal_counts: tuple[int, int],
    expected_score: tuple[int, int],
    expected_queries: int,
    django_assert_num_queries: Callable[[int], AbstractContextManager[None]],
) -> None:
    """Only active summaries aggregate shots; other states use persisted scores."""
    tracker = create_tracker_match(prefix="Summary", start_offset=timedelta())
    match_data = tracker.match_data
    match_data.status = status
    match_data.save(update_fields=["status"])
    player = create_tracker_player(username="scorer")
    for team, count in zip(
        (tracker.home_team, tracker.away_team), goal_counts, strict=True
    ):
        for _ in range(count):
            Shot.objects.create(
                match_data=match_data, team=team, player=player, scored=True
            )

    # Deliberately disagree with the shots in every state, including active matches.
    match_data.home_score = 7
    match_data.away_score = 4
    match_data.save(update_fields=["home_score", "away_score"])
    qs = MatchData.objects.select_related(
        "match_link",
        "match_link__home_team",
        "match_link__home_team__club",
        "match_link__away_team",
        "match_link__away_team__club",
        "match_link__season",
    ).filter(id_uuid=match_data.id_uuid)

    with django_assert_num_queries(1):
        entries = list(qs)
    with django_assert_num_queries(expected_queries):
        payload = build_match_summaries(entries)
    assert payload[0]["score"] == {"home": expected_score[0], "away": expected_score[1]}
