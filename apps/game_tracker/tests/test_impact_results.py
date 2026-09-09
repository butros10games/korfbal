"""Contracts for persisted impact precision and explanation assembly."""

from decimal import Decimal

import pytest

from apps.game_tracker.domain.impact_results import MatchImpactRow, build_impact_results
from apps.game_tracker.domain.impact_scoring import MatchImpactContribution


@pytest.mark.parametrize(
    ("points", "wpa", "score", "expected_wpa"),
    [
        (0.0003, 0.000003, "0.001", "0.00001"),
        (-0.0003, -0.000003, "-0.001", "-0.00001"),
        (0.00025, 0.0000025, "0.001", "0.00001"),
        (-0.00025, -0.0000025, "-0.001", "-0.00001"),
    ],
)
def test_rounding_follows_aggregation_and_preserves_breakdown_precision(
    points: float, wpa: float, score: str, expected_wpa: str
) -> None:
    """Small events accumulate before signed half-up storage rounding."""
    contributions = [
        MatchImpactContribution(
            player_id="player",
            time=time,
            category="possession_gain",
            points=points,
            source_type="possession_change",
            win_probability_added=wpa,
        )
        for time in ("1", "2")
    ]

    rows, breakdown = build_impact_results(
        contributions,
        known_player_ids=["player"],
        player_team_id={"player": "home"},
    )

    assert rows == [
        MatchImpactRow("player", "home", Decimal(score), Decimal(expected_wpa))
    ]
    assert breakdown == {
        "player": {"possession_gain": {"points": points * 2, "count": 2}}
    }


def test_results_include_roster_and_event_only_players_in_stable_order() -> None:
    """Keep zero-score roster rows and do not invent an unknown player's team."""
    rows, breakdown = build_impact_results(
        [
            MatchImpactContribution(
                player_id="event-only",
                time="1",
                category="offense_goal_above_expected",
                points=0.82,
                source_type="shot",
            )
        ],
        known_player_ids=["home-player", "away-player"],
        player_team_id={"home-player": "home", "away-player": "away"},
    )

    assert rows == [
        MatchImpactRow("home-player", "home", Decimal("0.000")),
        MatchImpactRow("away-player", "away", Decimal("0.000")),
        MatchImpactRow("event-only", None, Decimal("0.820")),
    ]
    assert breakdown == {
        "event-only": {"offense_goal_above_expected": {"points": 0.82, "count": 1}}
    }


def test_empty_match_has_no_invented_contributions() -> None:
    """An empty timeline keeps only explicitly known players, without categories."""
    assert build_impact_results([], known_player_ids=[], player_team_id={}) == ([], {})
    assert build_impact_results(
        [], known_player_ids=["player"], player_team_id={"player": "home"}
    ) == ([MatchImpactRow("player", "home", Decimal("0.000"))], {})
