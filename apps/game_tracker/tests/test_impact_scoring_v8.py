"""Unit tests for v8 possession value added."""

from __future__ import annotations

import pytest

from apps.game_tracker.domain.impact_scoring import (
    V8_FAST_GOAL_BONUS,
    V8_POSSESSION_VALUE,
    aggregate_v7_contributions,
    compute_v8_contributions,
)


def test_v8_combines_shots_and_attributed_possession_changes() -> None:
    """Possession events use the open-play value and preserve goal units."""
    contributions = compute_v8_contributions(
        [
            {
                "player_id": "attacker",
                "shot_type": "Afstand schot",
                "scored": False,
                "for_team": True,
                "time": "1",
            }
        ],
        [
            {
                "type": "possession_change",
                "kind": "ball_loss",
                "player_id": "attacker",
                "time": "2",
            },
            {
                "type": "possession_change",
                "kind": "interception",
                "player_id": "defender",
                "time": "3",
            },
        ],
    )

    assert pytest.approx(0.18) == V8_POSSESSION_VALUE
    assert [item.category for item in contributions] == [
        "offense_miss_below_expected",
        "possession_loss",
        "possession_gain",
    ]
    assert aggregate_v7_contributions(contributions) == pytest.approx({
        "attacker": -0.36,
        "defender": 0.18,
    })
    assert contributions[1].source_type == "possession_change"
    assert contributions[1].possession_kind == "ball_loss"
    assert contributions[1].expected_goals is None
    assert contributions[1].base_points == pytest.approx(-0.18)
    assert contributions[1].leverage_multiplier == pytest.approx(1.0)
    assert contributions[1].transition_bonus == pytest.approx(0.0)


def test_v8_ignores_unknown_or_unattributed_possession_events() -> None:
    """Team-only tracker events must not be invented as individual impact."""
    contributions = compute_v8_contributions(
        [],
        [
            {"type": "possession_change", "kind": "ball_loss", "player_id": None},
            {
                "type": "possession_change",
                "kind": "unknown",
                "player_id": "player",
            },
            {"type": "pause", "kind": "interception", "player_id": "player"},
        ],
    )

    assert contributions == []


def test_v8_rewards_an_interception_followed_by_a_fast_same_team_goal() -> None:
    """A ten-second conversion adds only a partial creation credit."""
    contributions = compute_v8_contributions(
        [],
        [
            {
                "type": "possession_change",
                "kind": "interception",
                "player_id": "interceptor",
                "team_id": "home",
                "match_part_id": "second-half",
                "time": "40",
                "time_iso": "2026-01-01T12:40:00+00:00",
            },
            {
                "type": "goal",
                "event_id": "fast-goal",
                "team_id": "home",
                "match_part_id": "second-half",
                "time": "40",
                "time_iso": "2026-01-01T12:40:08+00:00",
            },
        ],
    )

    contribution = contributions[0]
    assert pytest.approx(0.09) == V8_FAST_GOAL_BONUS
    assert contribution.base_points == pytest.approx(0.18)
    assert contribution.transition_bonus == pytest.approx(0.09)
    assert contribution.leverage_multiplier == pytest.approx(1.0)
    assert contribution.points == pytest.approx(0.27)
    assert contribution.linked_goal_event_id == "fast-goal"


def test_v8_fast_goal_bonus_ends_when_the_team_loses_possession() -> None:
    """A later goal is unrelated after a loss by the intercepting team."""
    contributions = compute_v8_contributions(
        [],
        [
            {
                "type": "possession_change",
                "kind": "interception",
                "player_id": "interceptor",
                "team_id": "home",
                "time": "40",
                "time_iso": "2026-01-01T12:40:00+00:00",
            },
            {
                "type": "possession_change",
                "kind": "ball_loss",
                "player_id": "teammate",
                "team_id": "home",
                "time": "40",
                "time_iso": "2026-01-01T12:40:04+00:00",
            },
            {
                "type": "goal",
                "event_id": "unrelated-goal",
                "team_id": "home",
                "time": "40",
                "time_iso": "2026-01-01T12:40:08+00:00",
            },
        ],
    )

    interception = contributions[0]
    assert interception.points == pytest.approx(0.18)
    assert interception.transition_bonus == pytest.approx(0.0)
    assert interception.linked_goal_event_id is None


@pytest.mark.parametrize(
    ("score_margin", "expected_multiplier"),
    [(0, 1.75), (1, 1.5), (2, 1.25), (3, 1.0)],
)
def test_v8_weights_final_five_minute_possessions_by_score_margin(
    score_margin: int,
    expected_multiplier: float,
) -> None:
    """Close late-game possession changes receive bounded match leverage."""
    goals = [
        {
            "type": "goal",
            "event_id": f"home-goal-{index}",
            "team_id": "home",
            "time": str(index + 1),
            "time_iso": f"2026-01-01T12:00:{index:02d}+00:00",
        }
        for index in range(score_margin)
    ]
    contributions = compute_v8_contributions(
        [],
        [
            *goals,
            {
                "type": "possession_change",
                "kind": "ball_loss",
                "player_id": "late-loser",
                "team_id": "away",
                "time": "56",
                "time_iso": "2026-01-01T12:56:00+00:00",
            },
        ],
        match_duration_minutes=60,
    )

    contribution = contributions[0]
    assert contribution.leverage_multiplier == expected_multiplier
    assert contribution.points == pytest.approx(-0.18 * expected_multiplier)


def test_v8_does_not_apply_close_game_leverage_before_final_five_minutes() -> None:
    """An otherwise identical tied event remains context-neutral earlier."""
    contributions = compute_v8_contributions(
        [],
        [
            {
                "type": "possession_change",
                "kind": "interception",
                "player_id": "interceptor",
                "team_id": "home",
                "time": "54",
            }
        ],
        match_duration_minutes=60,
    )

    assert contributions[0].leverage_multiplier == pytest.approx(1.0)
    assert contributions[0].points == pytest.approx(0.18)
