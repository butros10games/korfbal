"""Unit tests for the framework-independent v7 scoring policy."""

from __future__ import annotations

import pytest

from apps.game_tracker.domain.impact_scoring import (
    aggregate_v7_contributions,
    compute_v7_contributions,
    expected_goal_probability,
)


@pytest.mark.parametrize(
    ("shot_type", "expected"),
    [
        (None, 0.18),
        ("Afstand schot", 0.18),
        ("Doorloopbal", 0.18),
        ("Vrije bal", 0.35),
        ("Strafworp", 0.75),
    ],
)
def test_expected_goal_probability_uses_stable_baselines(
    shot_type: str | None, expected: float
) -> None:
    """Set pieces differ while open-play types share a robust baseline."""
    assert expected_goal_probability(shot_type) == expected


def test_v7_is_symmetric_for_attacker_and_responsible_defender() -> None:
    """Equivalent attacking and defending outcomes have opposite values."""
    contributions = compute_v7_contributions([
        {
            "player_id": "attacker",
            "shot_type": "Afstand schot",
            "scored": True,
            "for_team": True,
            "time": "1",
        },
        {
            "player_id": "defender",
            "shot_type": "Afstand schot",
            "scored": True,
            "for_team": False,
            "time": "1",
        },
        {
            "player_id": "defender",
            "shot_type": "Afstand schot",
            "scored": False,
            "for_team": False,
            "time": "2",
        },
    ])

    assert contributions[0].points == pytest.approx(0.82)
    assert contributions[1].points == pytest.approx(-0.82)
    assert contributions[2].points == pytest.approx(0.18)
    assert aggregate_v7_contributions(contributions) == pytest.approx({
        "attacker": 0.82,
        "defender": -0.64,
    })
