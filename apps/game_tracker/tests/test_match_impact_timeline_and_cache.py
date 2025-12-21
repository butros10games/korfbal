"""Deep regression/edge-case tests for match impact timeline + caching.

The match impact implementation in `apps.game_tracker.services.match_impact`
mirrors korfbal-web semantics. The most fragile parts tend to be:

- role timeline reconstruction from substitutions (grouped per minute)
- cache behavior for expensive breakdown computations
- shooter-efficiency multipliers (must ignore defensive Shot rows)

These tests are designed to lock in intended behavior under messy/realistic
inputs.
"""

from __future__ import annotations

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.utils import timezone
import pytest
from pytest_django.fixtures import SettingsWrapper

from apps.club.models import Club
from apps.game_tracker.models import GroupType, MatchData, PlayerGroup
from apps.game_tracker.services import match_impact
from apps.schedule.models import Match, Season
from apps.team.models import Team


@pytest.mark.django_db
def test_build_match_player_role_timeline_substitution_swaps_roles_at_minute() -> None:
    """Substitutions should flip roles exactly at the integer-minute boundary.

    Intended behavior:
        - Player OUT keeps their current role until the substitution minute,
            then becomes reserve.
        - Player IN starts as reserve (inferred) and becomes the group role from
            that minute.

    This validates the backwards-inference + forwards-application algorithm.
    """
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

    # End-of-match state: group contains the player who was substituted IN.
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

    timeline = match_impact.build_match_player_role_timeline(
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

    assert out_intervals.aanval[0].start == 0.0
    assert out_intervals.aanval[0].end == float(sub_minute)
    assert out_intervals.reserve[0].start == float(sub_minute)
    assert out_intervals.reserve[0].end == match_end

    assert in_intervals.reserve[0].start == 0.0
    assert in_intervals.reserve[0].end == float(sub_minute)
    assert in_intervals.aanval[0].start == float(sub_minute)
    assert in_intervals.aanval[0].end == match_end


def test_compute_shooting_efficiency_multipliers_ignores_defensive_shots() -> None:
    """Defensive shot rows (for_team=False) must not affect shooter efficiency.

    Regression intent (v4+ semantics):
    - for_team=True shots count toward attempts/goals for the shooter.
    - for_team=False rows represent defensive tracking and must be ignored.

    This test constructs a case where counting defensive rows would change the
    efficiency band.
    """
    shooter_id = "p1"

    # Offensive attempts: 5, goals: 2 => 0.4 => GOOD => (1.1, 0.85)
    shots: list[dict[str, object]] = [
        {"player_id": shooter_id, "for_team": True, "scored": True} for _ in range(2)
    ] + [{"player_id": shooter_id, "for_team": True, "scored": False} for _ in range(3)]

    # Defensive tracking row that must be ignored.
    shots.append({"player_id": shooter_id, "for_team": False, "scored": True})

    goal_mult, miss_mult = match_impact._compute_shooting_efficiency_multipliers(
        shots=shots,
        algorithm_version="v5",
    )

    assert goal_mult[shooter_id] == pytest.approx(1.1)
    assert miss_mult[shooter_id] == pytest.approx(0.85)


@pytest.mark.django_db
def test_compute_match_impact_breakdown_cached_returns_cached_dict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If a dict is cached, the cached path must be used and compute skipped."""
    today = timezone.now().date()
    season = Season.objects.create(name="2025", start_date=today, end_date=today)
    home_team = Team.objects.create(name="Home", club=Club.objects.create(name="HC"))
    away_team = Team.objects.create(name="Away", club=Club.objects.create(name="AC"))
    match = Match.objects.create(
        home_team=home_team,
        away_team=away_team,
        season=season,
        start_time=timezone.now(),
    )
    match_data = MatchData.objects.get(match_link=match)

    cached_value: dict[str, object] = {
        "player": {"goal_scored": {"points": 1.0, "count": 1}}
    }

    monkeypatch.setattr(match_impact.cache, "get", lambda _k: cached_value)

    def boom(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("compute_match_impact_breakdown should not be called")

    monkeypatch.setattr(match_impact, "compute_match_impact_breakdown", boom)

    result = match_impact.compute_match_impact_breakdown_cached(match_data=match_data)
    assert result is cached_value


@pytest.mark.django_db
def test_compute_match_impact_breakdown_cached_handles_cache_errors(
    monkeypatch: pytest.MonkeyPatch,
    settings: SettingsWrapper,
) -> None:
    """Cache backend failures must not prevent computing/returning breakdowns."""
    _ = settings

    today = timezone.now().date()
    season = Season.objects.create(name="2025", start_date=today, end_date=today)
    home_team = Team.objects.create(name="Home", club=Club.objects.create(name="HC"))
    away_team = Team.objects.create(name="Away", club=Club.objects.create(name="AC"))
    match = Match.objects.create(
        home_team=home_team,
        away_team=away_team,
        season=season,
        start_time=timezone.now(),
    )
    match_data = MatchData.objects.get(match_link=match)

    def failing_get(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("cache down")

    monkeypatch.setattr(match_impact.cache, "get", failing_get)

    computed_breakdown: dict[str, object] = {
        "x": {"shot_miss_for": {"points": -0.6, "count": 1}}
    }

    monkeypatch.setattr(
        match_impact,
        "compute_match_impact_breakdown",
        lambda **_kwargs: ([], computed_breakdown),
    )

    # Even if set fails, result must still be returned.
    def failing_set(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("cache set down")

    monkeypatch.setattr(match_impact.cache, "set", failing_set)

    result = match_impact.compute_match_impact_breakdown_cached(match_data=match_data)
    assert result == computed_breakdown
