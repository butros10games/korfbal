"""Tests for goal streak bonus capping.

The match impact algorithm applies a *team* goal streak multiplier.
To keep goal impact totals intuitive, we cap the maximum streak bonus.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from apps.game_tracker.models import GoalType, Shot
from apps.game_tracker.services.match_impact import (
    compute_match_impact_breakdown,
)
from apps.game_tracker.tests.tracker_test_helpers import (
    create_match_part,
    create_tracker_match,
    create_tracker_player,
)


GOAL_COUNT = 6


@pytest.mark.django_db
def test_goal_points_streak_bonus_is_capped() -> None:
    """Long scoring streaks should not increase goal points without bound."""
    tracker = create_tracker_match(prefix="Impact", start_offset=-timedelta(minutes=30))
    match_data = tracker.match_data
    match_data.status = "finished"
    match_data.save(update_fields=["status"])

    part = create_match_part(match_data=match_data, start_offset=-timedelta(minutes=10))
    part_start = part.start_time
    assert part_start is not None

    # "doorloop" => type weight 1.25.
    goal_type = GoalType.objects.create(name="Doorloopbal")

    scorer = create_tracker_player(username="streak_scorer")

    # Create consecutive goals by the same team and player.
    for i in range(GOAL_COUNT):
        Shot.objects.create(
            player=scorer,
            match_data=match_data,
            match_part=part,
            team=tracker.home_team,
            for_team=True,
            scored=True,
            shot_type=goal_type,
            time=part_start + timedelta(minutes=i + 1),
        )

    _rows, breakdown = compute_match_impact_breakdown(
        match_data=match_data,
        algorithm_version="v6",
    )

    per_player = breakdown[str(scorer.id_uuid)]
    assert per_player["goal_scored"]["count"] == GOAL_COUNT

    # Expected with capped streak (max streak=4):
    # base = 3.2 * 1.25 = 4.0
    # streak factors: 1.00, 1.12, 1.24, 1.36, 1.36, 1.36
    expected_total = 4.0 * (1.00 + 1.12 + 1.24 + 1.36 + 1.36 + 1.36)

    assert per_player["goal_scored"]["points"] == pytest.approx(expected_total)
