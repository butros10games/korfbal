"""Regression tests for attacker/defender attribution in impact scoring."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from apps.game_tracker.models import GoalType, Shot
from apps.game_tracker.services.match_impact import (
    LATEST_MATCH_IMPACT_ALGORITHM_VERSION,
    compute_match_impact_breakdown,
)
from apps.game_tracker.tests.tracker_test_helpers import (
    create_match_part,
    create_tracker_match,
    create_tracker_player,
)


@pytest.mark.django_db
def test_v7_scores_for_team_false_as_direct_defensive_responsibility() -> None:
    """A conceded goal must hurt the selected defender, not credit a scorer."""
    tracker = create_tracker_match(prefix="Impact", start_offset=-timedelta(minutes=30))
    match_data = tracker.match_data
    match_data.status = "finished"
    match_data.save(update_fields=["status"])

    part = create_match_part(match_data=match_data, start_offset=-timedelta(minutes=10))
    part_start = part.start_time
    assert part_start is not None

    goal_type = GoalType.objects.create(name="Doorloopbal")

    scorer = create_tracker_player(username="scorer")

    defender = create_tracker_player(username="defender")

    # A real scored goal for the shooter's team.
    Shot.objects.create(
        player=scorer,
        match_data=match_data,
        match_part=part,
        team=tracker.home_team,
        for_team=True,
        scored=True,
        shot_type=goal_type,
        time=part_start + timedelta(minutes=1),
    )

    # A conceded goal tracked against a defending player.
    Shot.objects.create(
        player=defender,
        match_data=match_data,
        match_part=part,
        team=tracker.home_team,
        for_team=False,
        scored=True,
        shot_type=goal_type,
        time=part_start + timedelta(minutes=2),
    )

    rows, breakdown = compute_match_impact_breakdown(
        match_data=match_data,
        algorithm_version=LATEST_MATCH_IMPACT_ALGORITHM_VERSION,
    )

    scorer_key = str(scorer.id_uuid)
    defender_key = str(defender.id_uuid)

    rows_by_player = {row.player_id: row for row in rows}
    assert breakdown[scorer_key]["offense_goal_above_expected"]["count"] == 1
    assert rows_by_player[scorer_key].impact_score == Decimal("0.820")
    assert breakdown[defender_key]["defense_goal_below_expected"]["count"] == 1
    assert rows_by_player[defender_key].impact_score == Decimal("-0.820")

    _legacy_rows, legacy_breakdown = compute_match_impact_breakdown(
        match_data=match_data,
        algorithm_version="v6",
    )
    assert legacy_breakdown[defender_key]["goal_scored"]["count"] == 1
