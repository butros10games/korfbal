"""Tests for Korfbal win probability and event attribution."""

from __future__ import annotations

import pytest

from apps.game_tracker.domain.impact_scoring import compute_v8_contributions
from apps.game_tracker.domain.win_probability import match_outcome_probabilities


def test_terminal_outcomes_preserve_win_draw_and_loss() -> None:
    """No time remaining produces a deterministic result."""
    home_win = match_outcome_probabilities(
        home_score=12,
        away_score=11,
        seconds_remaining=0,
        possession="away",
    )
    draw = match_outcome_probabilities(
        home_score=12,
        away_score=12,
        seconds_remaining=0,
        possession="home",
    )

    assert home_win.home_win == pytest.approx(1.0)
    assert home_win.draw == pytest.approx(0.0)
    assert draw.draw == pytest.approx(1.0)
    assert draw.expectancy("home") == pytest.approx(0.5)
    assert draw.expectancy("away") == pytest.approx(0.5)


def test_outcome_probabilities_are_normalized_and_symmetric() -> None:
    """Both teams' expected result shares always sum to one."""
    probabilities = match_outcome_probabilities(
        home_score=18,
        away_score=17,
        seconds_remaining=420,
        possession="away",
    )

    assert (
        probabilities.home_win + probabilities.draw + probabilities.away_win
    ) == pytest.approx(1.0)
    assert probabilities.expectancy("home") + probabilities.expectancy(
        "away"
    ) == pytest.approx(1.0)


def test_possession_is_more_valuable_late_in_a_tied_match() -> None:
    """The model creates leverage naturally without time multipliers."""

    def possession_swing(seconds_remaining: int) -> float:
        home_possession = match_outcome_probabilities(
            home_score=20,
            away_score=20,
            seconds_remaining=seconds_remaining,
            possession="home",
        ).expectancy("home")
        away_possession = match_outcome_probabilities(
            home_score=20,
            away_score=20,
            seconds_remaining=seconds_remaining,
            possession="away",
        ).expectancy("home")
        return home_possession - away_possession

    assert possession_swing(30) > possession_swing(20 * 60)
    assert possession_swing(30) > 0


def test_goal_wpa_is_zero_sum_between_scorer_and_responsible_defender() -> None:
    """The same goal helps the scorer exactly as much as it hurts the defender."""
    shots = [
        {
            "event_id": "goal-event",
            "player_id": "scorer",
            "team_id": "home",
            "shot_type": "Afstand schot",
            "scored": True,
            "for_team": True,
            "time": "60",
        },
        {
            "event_id": "goal-event",
            "player_id": "defender",
            "team_id": "home",
            "shot_type": "Afstand schot",
            "scored": True,
            "for_team": False,
            "time": "60",
        },
    ]
    events = [
        {
            "type": "goal",
            "event_id": "goal-event",
            "player_id": "scorer",
            "team_id": "home",
            "elapsed_seconds": 3590,
            "time": "60",
        }
    ]

    contributions = compute_v8_contributions(
        shots,
        events,
        match_duration_minutes=60,
        home_team_id="home",
        away_team_id="away",
    )
    by_player = {item.player_id: item for item in contributions}

    assert by_player["scorer"].win_probability_added > 0
    assert by_player["defender"].win_probability_added < 0
    assert (
        by_player["scorer"].win_probability_added
        + by_player["defender"].win_probability_added
    ) == pytest.approx(0.0)
    assert by_player["scorer"].win_expectancy_before is not None
    assert by_player["scorer"].win_expectancy_after is not None


def test_late_interception_receives_positive_wpa_without_a_later_goal() -> None:
    """Possession itself changes win expectancy in the new tracker data."""
    contributions = compute_v8_contributions(
        [],
        [
            {
                "type": "possession_change",
                "event_id": "interception-event",
                "kind": "interception",
                "player_id": "interceptor",
                "team_id": "home",
                "elapsed_seconds": 3570,
                "time": "60",
            }
        ],
        match_duration_minutes=60,
        home_team_id="home",
        away_team_id="away",
    )

    contribution = contributions[0]
    assert contribution.win_probability_added > 0
    assert contribution.win_expectancy_after > contribution.win_expectancy_before
