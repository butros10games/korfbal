"""Characterization tests for the legacy v6 per-team impact features."""

from __future__ import annotations

from datetime import timedelta

import pytest

from apps.game_tracker.models import GoalType, Shot
from apps.game_tracker.services.match_impact import compute_match_team_impact_features
from apps.game_tracker.tests.tracker_test_helpers import (
    create_group_types,
    create_match_part,
    create_player_group,
    create_tracker_match,
    create_tracker_player,
)


@pytest.mark.django_db
def test_team_features_split_shots_goals_and_doorloop_concessions() -> None:
    """Shots, goals and doorloop concessions accumulate on the correct team."""
    tracker = create_tracker_match(prefix="Features", start_offset=-timedelta(hours=1))
    match_data = tracker.match_data
    part = create_match_part(match_data=match_data, start_offset=-timedelta(minutes=30))
    assert part.start_time is not None

    home_attacker = create_tracker_player(username="home-attacker")
    away_defender = create_tracker_player(username="away-defender")
    group_types = create_group_types("Aanval", "Verdediging")
    create_player_group(
        match_data=match_data, team=tracker.home_team, group_type=group_types["Aanval"]
    ).players.add(home_attacker)
    create_player_group(
        match_data=match_data,
        team=tracker.away_team,
        group_type=group_types["Verdediging"],
    ).players.add(away_defender)

    doorloop = GoalType.objects.create(name="Doorloopbal")
    shots = [(False, None), (True, doorloop), (True, doorloop), (False, None)]
    for minute, (scored, shot_type) in enumerate(shots, start=1):
        Shot.objects.create(
            player=home_attacker,
            match_data=match_data,
            match_part=part,
            team=tracker.home_team,
            for_team=True,
            scored=scored,
            shot_type=shot_type,
            time=part.start_time + timedelta(minutes=minute),
        )

    features = compute_match_team_impact_features(match_data=match_data)

    home = features[str(tracker.home_team.id_uuid)]
    away = features[str(tracker.away_team.id_uuid)]
    assert home.shooter_misses_weighted == pytest.approx(2.0)
    assert home.goals_scored_points > 0
    assert (home.defended_shots, home.defended_goals, home.defended_misses) == (
        0,
        0,
        0,
    )
    assert home.doorloop_concede_points_times_defenders == 0
    # Roles switch after two goals, so the final miss has no away defender.
    assert (away.defended_shots, away.defended_goals, away.defended_misses) == (
        3,
        2,
        1,
    )
    assert away.goals_scored_points == 0
    assert away.doorloop_concede_points_times_defenders == pytest.approx(
        home.goals_scored_points
    )
