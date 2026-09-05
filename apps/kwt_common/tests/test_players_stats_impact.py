"""Tests for team/season player stats impact aggregation.

These tests ensure that the Team page "impact" totals are consistent with the
Match page algorithm by recomputing persisted per-match impact rows when needed.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from asgiref.sync import async_to_sync
import pytest

from apps.game_tracker.models import (
    GoalType,
    MatchData,
    PlayerMatchImpact,
    Shot,
)
from apps.game_tracker.services.match_impact import (
    LATEST_MATCH_IMPACT_ALGORITHM_VERSION,
)
from apps.game_tracker.tests.tracker_test_helpers import (
    create_match_part,
    create_tracker_match,
    create_tracker_player,
)
from apps.kwt_common.utils.players_stats import build_player_stats


EXPECTED_SINGLE_MISS_IMPACT = -0.2
EXPECTED_SINGLE_MISS_STORED = -0.18
EXPECTED_FIVE_MISSES_IMPACT = -0.9


@pytest.mark.django_db
def test_build_player_stats_recomputes_outdated_match_impacts() -> None:
    """If impacts exist only at an older algorithm version, recompute to latest."""
    tracker = create_tracker_match(
        prefix="Impact recompute", start_offset=-timedelta(minutes=30)
    )
    match_data = tracker.match_data
    home_team = tracker.home_team
    match_data.status = "finished"
    match_data.save(update_fields=["status"])

    part = create_match_part(
        match_data=match_data, start_offset=-timedelta(minutes=10), active=True
    )
    part_start = part.start_time

    player = create_tracker_player(username="impact_recompute")

    # The stored -0.18 open-play miss is presented as -0.2 on the Team page.
    Shot.objects.create(
        player=player,
        match_data=match_data,
        match_part=part,
        team=home_team,
        scored=False,
        time=part_start + timedelta(minutes=1),
    )

    # Seed an outdated persisted impact row that must be replaced.
    PlayerMatchImpact.objects.update_or_create(
        match_data=match_data,
        player=player,
        defaults={
            "team": home_team,
            "impact_score": Decimal("5.0"),
            "algorithm_version": "v0",
        },
    )

    # build_player_stats should recompute the match impacts to latest and return them.
    rows = async_to_sync(build_player_stats)(
        [player],
        MatchData.objects.filter(id_uuid=match_data.id_uuid),
    )

    assert len(rows) == 1
    assert rows[0]["username"] == "impact_recompute"
    assert rows[0]["impact_score"] == EXPECTED_SINGLE_MISS_IMPACT
    assert rows[0]["win_probability_added"] == pytest.approx(0.0)
    assert rows[0]["impact_is_stored"] is True

    updated = PlayerMatchImpact.objects.get(match_data=match_data, player=player)
    assert updated.algorithm_version == LATEST_MATCH_IMPACT_ALGORITHM_VERSION
    assert float(updated.impact_score) == pytest.approx(EXPECTED_SINGLE_MISS_STORED)


@pytest.mark.django_db
def test_build_player_stats_aggregates_stored_goal_wpa() -> None:
    """Team-season rows include the persisted sum of event-level WPA."""
    tracker = create_tracker_match(
        prefix="WPA aggregation", start_offset=-timedelta(hours=2)
    )
    match_data = tracker.match_data
    home_team = tracker.home_team
    match_data.status = "finished"
    match_data.parts = 1
    match_data.part_length = 3600
    match_data.save(update_fields=["status", "parts", "part_length"])
    part = create_match_part(
        match_data=match_data, start_offset=-timedelta(hours=1), active=False
    )
    part_start = part.start_time
    player = create_tracker_player(username="wpa_scorer")
    Shot.objects.create(
        player=player,
        match_data=match_data,
        match_part=part,
        team=home_team,
        shot_type=GoalType.objects.create(name="Afstand schot"),
        for_team=True,
        scored=True,
        time=part_start + timedelta(seconds=3590),
    )

    rows = async_to_sync(build_player_stats)(
        [player],
        MatchData.objects.filter(id_uuid=match_data.id_uuid),
    )

    assert len(rows) == 1
    assert rows[0]["win_probability_added"] is not None
    assert rows[0]["win_probability_added"] > 0
    persisted = PlayerMatchImpact.objects.get(match_data=match_data, player=player)
    assert persisted.win_probability_added > 0


@pytest.mark.django_db
def test_build_player_stats_five_misses_uses_latest_weights() -> None:
    """With 5 misses, the latest weights should be applied consistently."""
    tracker = create_tracker_match(
        prefix="Five misses", start_offset=-timedelta(minutes=30)
    )
    match_data = tracker.match_data
    home_team = tracker.home_team
    match_data.status = "finished"
    match_data.save(update_fields=["status"])

    part = create_match_part(
        match_data=match_data, start_offset=-timedelta(minutes=10), active=True
    )
    part_start = part.start_time

    player = create_tracker_player(username="impact_eff_v3")

    # v7: 5 open-play misses at -0.18 each => -0.9.
    for i in range(5):
        Shot.objects.create(
            player=player,
            match_data=match_data,
            match_part=part,
            team=home_team,
            scored=False,
            time=part_start + timedelta(minutes=i + 1),
        )

    # Force recomputation.
    PlayerMatchImpact.objects.update_or_create(
        match_data=match_data,
        player=player,
        defaults={
            "team": home_team,
            "impact_score": Decimal("0.0"),
            "algorithm_version": "v0",
        },
    )

    rows = async_to_sync(build_player_stats)(
        [player],
        MatchData.objects.filter(id_uuid=match_data.id_uuid),
    )

    assert len(rows) == 1
    assert rows[0]["username"] == "impact_eff_v3"
    assert rows[0]["impact_score"] == EXPECTED_FIVE_MISSES_IMPACT
    assert rows[0]["impact_is_stored"] is True

    updated = PlayerMatchImpact.objects.get(match_data=match_data, player=player)
    assert updated.algorithm_version == LATEST_MATCH_IMPACT_ALGORITHM_VERSION
    assert float(updated.impact_score) == pytest.approx(EXPECTED_FIVE_MISSES_IMPACT)
