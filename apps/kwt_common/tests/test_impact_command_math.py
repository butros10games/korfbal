"""Regression tests for the impact audit and fitting command math."""

from __future__ import annotations

import pytest

from apps.game_tracker.services.match_impact import MatchTeamImpactFeatures
from apps.kwt_common.management.commands import fit_match_impact_v6
from apps.kwt_common.management.commands.audit_impact_scores import (
    spearman_rho,
    team_page_heuristic_impact,
)


def test_spearman_rho_handles_ties_and_rejects_mismatched_samples() -> None:
    """Spearman calculation supports ties and validates sample shape."""
    assert spearman_rho([1, 1, 3], [3, 3, 1]) == pytest.approx(-1.0)
    assert spearman_rho([1], [1]) is None
    assert spearman_rho([1, 1], [2, 2]) is None

    with pytest.raises(ValueError, match="same length"):
        spearman_rho([1], [1, 2])


def test_team_heuristic_is_safe_when_no_shots_exist() -> None:
    """Zero denominators remain finite while real shot accuracy contributes."""
    expected_scoring_impact = 15.5
    assert team_page_heuristic_impact(gf=0, ga=0, sf=0, sa=0) == pytest.approx(0.0)
    assert team_page_heuristic_impact(gf=1, ga=0, sf=2, sa=0) == pytest.approx(
        expected_scoring_impact
    )


def test_fit_metrics_handle_empty_constant_and_draw_samples() -> None:
    """Degenerate tuning data returns stable metrics and excludes draws."""
    assert fit_match_impact_v6._pearson([], []) == pytest.approx(0.0)
    assert fit_match_impact_v6._pearson([1.0], [1.0, 2.0]) == pytest.approx(0.0)
    assert fit_match_impact_v6._pearson([1.0, 1.0], [2.0, 3.0]) == pytest.approx(0.0)
    assert fit_match_impact_v6._sign_accuracy(
        [1.0, -1.0, 5.0], [2.0, -3.0, 0.0]
    ) == pytest.approx(1.0)
    assert fit_match_impact_v6._sign_accuracy([1.0], [0.0]) == pytest.approx(0.0)


def test_kfold_splits_cover_each_row_once_as_validation() -> None:
    """Every row is held out exactly once even when K exceeds row count."""
    row_count = 5
    splits = fit_match_impact_v6._kfold_splits(n=row_count, k=99)

    assert len(splits) == row_count
    assert sorted(index for _train, valid in splits for index in valid) == list(
        range(row_count)
    )
    for train, valid in splits:
        assert set(train).isdisjoint(valid)
        assert sorted(train + valid) == list(range(row_count))


def test_candidate_evaluation_uses_home_minus_away_impact() -> None:
    """Candidate quality uses the same home-minus-away direction as outcomes."""
    empty = MatchTeamImpactFeatures(
        team_id="away",
        goals_scored_points=0.0,
        shooter_misses_weighted=0.0,
        defended_shots=0,
        defended_goals=0,
        defended_misses=0,
        doorloop_concede_points_times_defenders=0.0,
    )
    scoring_home = MatchTeamImpactFeatures(
        team_id="home",
        goals_scored_points=8.0,
        shooter_misses_weighted=0.0,
        defended_shots=0,
        defended_goals=0,
        defended_misses=0,
        doorloop_concede_points_times_defenders=0.0,
    )
    rows: list[dict[str, object]] = [
        {"features_home": scoring_home, "features_away": empty, "goal_diff": 2.0},
        {"features_home": empty, "features_away": scoring_home, "goal_diff": -1.0},
    ]
    candidate = fit_match_impact_v6.CandidateWeights(
        miss_for_penalty=0.5,
        shot_against_total=-1.0,
        goal_against_total=-5.0,
        miss_against_total=0.5,
        doorloop_concede_factor=0.06,
    )

    assert fit_match_impact_v6._eval_candidate(rows, candidate) == {
        "pearson": pytest.approx(1.0),
        "sign_acc": 1.0,
    }
