"""Caching regression tests for persisted match impact breakdowns."""

from __future__ import annotations

from django.utils import timezone
import pytest
from pytest_django.fixtures import SettingsWrapper

from apps.club.models import Club
from apps.game_tracker.models import MatchData
from apps.game_tracker.services import match_impact_persistence
from apps.schedule.models import Match, Season
from apps.team.models import Team


def _create_match_data() -> MatchData:
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
    return MatchData.objects.get(match_link=match)


@pytest.mark.django_db
def test_compute_match_impact_breakdown_cached_returns_cached_dict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If a dict is cached, the cached path must be used and compute skipped."""
    match_data = _create_match_data()
    cached_value: dict[str, object] = {
        "player": {"goal_scored": {"points": 1.0, "count": 1}}
    }

    monkeypatch.setattr(match_impact_persistence.cache, "get", lambda _k: cached_value)

    def boom(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("compute_match_impact_breakdown should not be called")

    monkeypatch.setattr(
        match_impact_persistence, "compute_match_impact_breakdown", boom
    )

    result = match_impact_persistence.compute_match_impact_breakdown_cached(
        match_data=match_data
    )
    assert result is cached_value


@pytest.mark.django_db
def test_compute_match_impact_breakdown_cached_handles_cache_errors(
    monkeypatch: pytest.MonkeyPatch,
    settings: SettingsWrapper,
) -> None:
    """Cache backend failures must not prevent computing/returning breakdowns."""
    _ = settings
    match_data = _create_match_data()

    def failing_get(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("cache down")

    monkeypatch.setattr(match_impact_persistence.cache, "get", failing_get)

    computed_breakdown: dict[str, object] = {
        "x": {"shot_miss_for": {"points": -0.6, "count": 1}}
    }

    monkeypatch.setattr(
        match_impact_persistence,
        "compute_match_impact_breakdown",
        lambda **_kwargs: ([], computed_breakdown),
    )

    def failing_set(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("cache set down")

    monkeypatch.setattr(match_impact_persistence.cache, "set", failing_set)

    result = match_impact_persistence.compute_match_impact_breakdown_cached(
        match_data=match_data
    )
    assert result == computed_breakdown
