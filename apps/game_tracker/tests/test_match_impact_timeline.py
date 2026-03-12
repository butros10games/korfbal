"""Pure timeline/scorer regression tests for match impact helpers."""

from __future__ import annotations

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.utils import timezone
import pytest

from apps.club.models import Club
from apps.game_tracker.models import GroupType, MatchData, PlayerGroup
from apps.game_tracker.services import match_impact
from apps.game_tracker.services.match_impact_timeline import (
    build_match_player_role_timeline,
)
from apps.schedule.models import Match, Season
from apps.team.models import Team


@pytest.mark.django_db
def test_build_match_player_role_timeline_substitution_swaps_roles_at_minute() -> None:
    """Substitutions should flip roles exactly at the integer-minute boundary."""
    today = timezone.now().date()
    season = Season.objects.create(name="2025", start_date=today, end_date=today)

    home_team = Team.objects.create(name="Home", club=Club.objects.create(name="HC"))
    away_team = Team.objects.create(name="Away", club=Club.objects.create(name="AC"))

    match = Match.objects.create(
        home_team=home_team,
        away_team=away_team,
        season=season,
        start_time=timezone.now() - timedelta(minutes=30),
    )
    match_data = MatchData.objects.get(match_link=match)

    attack_type = GroupType.objects.create(name="Aanval", order=1)

    user_out = get_user_model().objects.create_user(username="p_out")
    user_in = get_user_model().objects.create_user(username="p_in")
    p_out = user_out.player
    p_in = user_in.player

    group = PlayerGroup.objects.create(
        match_data=match_data,
        team=home_team,
        starting_type=attack_type,
        current_type=attack_type,
    )
    group.players.add(p_in)

    sub_minute = 5
    match_end = 20.0

    events = [
        {
            "type": "substitute",
            "time": str(sub_minute),
            "player_in_id": str(p_in.id_uuid),
            "player_out_id": str(p_out.id_uuid),
            "player_group_id": str(group.id_uuid),
        }
    ]

    timeline = build_match_player_role_timeline(
        known_player_ids=[str(p_out.id_uuid), str(p_in.id_uuid)],
        groups=[group],
        events=events,
        match_end_minutes=match_end,
    )

    out_intervals = timeline[str(p_out.id_uuid)]
    in_intervals = timeline[str(p_in.id_uuid)]

    assert out_intervals.aanval
    assert out_intervals.reserve
    assert in_intervals.aanval
    assert in_intervals.reserve

    assert out_intervals.aanval[0].start == pytest.approx(0.0)
    assert out_intervals.aanval[0].end == pytest.approx(float(sub_minute))
    assert out_intervals.reserve[0].start == pytest.approx(float(sub_minute))
    assert out_intervals.reserve[0].end == pytest.approx(match_end)

    assert in_intervals.reserve[0].start == pytest.approx(0.0)
    assert in_intervals.reserve[0].end == pytest.approx(float(sub_minute))
    assert in_intervals.aanval[0].start == pytest.approx(float(sub_minute))
    assert in_intervals.aanval[0].end == pytest.approx(match_end)


def test_compute_shooting_efficiency_multipliers_ignores_defensive_shots() -> None:
    """Defensive shot rows (for_team=False) must not affect shooter efficiency."""
    shooter_id = "p1"
    shots: list[dict[str, object]] = [
        {"player_id": shooter_id, "for_team": True, "scored": True} for _ in range(2)
    ] + [{"player_id": shooter_id, "for_team": True, "scored": False} for _ in range(3)]
    shots.append({"player_id": shooter_id, "for_team": False, "scored": True})

    goal_mult, miss_mult = match_impact._compute_shooting_efficiency_multipliers(
        shots=shots,
        algorithm_version="v5",
    )

    assert goal_mult[shooter_id] == pytest.approx(1.1)
    assert miss_mult[shooter_id] == pytest.approx(0.85)
