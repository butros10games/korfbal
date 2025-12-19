"""Tests for match impacts schedule endpoint."""

from __future__ import annotations

from decimal import Decimal
from http import HTTPStatus

from django.contrib.auth import get_user_model
from django.test import override_settings
from django.test.client import Client
from django.utils import timezone
import pytest

from apps.club.models import Club
from apps.game_tracker.models import MatchData, PlayerMatchImpact
from apps.game_tracker.services.match_impact import (
    LATEST_MATCH_IMPACT_ALGORITHM_VERSION,
)
from apps.schedule.models import Match, Season
from apps.team.models import Team


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_match_impacts_returns_persisted_rows(client: Client) -> None:
    """The endpoint should return only the latest-version stored rows."""
    expected_home_impact = 1.2
    expected_away_impact = -0.6

    today = timezone.now().date()
    season = Season.objects.create(name="2025", start_date=today, end_date=today)

    home_club = Club.objects.create(name="Home Club")
    away_club = Club.objects.create(name="Away Club")
    home_team = Team.objects.create(name="Home Team", club=home_club)
    away_team = Team.objects.create(name="Away Team", club=away_club)

    match = Match.objects.create(
        home_team=home_team,
        away_team=away_team,
        season=season,
        start_time=timezone.now(),
    )

    match_data = MatchData.objects.get(match_link=match)
    match_data.status = "finished"
    match_data.save(update_fields=["status"])

    home_user = get_user_model().objects.create_user(
        username="home_player",
        password="pass1234",  # noqa: S106  # nosec
    )
    away_user = get_user_model().objects.create_user(
        username="away_player",
        password="pass1234",  # noqa: S106  # nosec
    )

    PlayerMatchImpact.objects.create(
        match_data=match_data,
        player=home_user.player,
        team=home_team,
        impact_score=Decimal("1.2"),
        algorithm_version=LATEST_MATCH_IMPACT_ALGORITHM_VERSION,
    )
    PlayerMatchImpact.objects.create(
        match_data=match_data,
        player=away_user.player,
        team=away_team,
        impact_score=Decimal("-0.6"),
        algorithm_version=LATEST_MATCH_IMPACT_ALGORITHM_VERSION,
    )
    # Noise row at older version should not be returned.
    legacy_user = get_user_model().objects.create_user(
        username="legacy_player",
        password="pass1234",  # noqa: S106  # nosec
    )
    PlayerMatchImpact.objects.create(
        match_data=match_data,
        player=legacy_user.player,
        team=home_team,
        impact_score=Decimal("9.9"),
        algorithm_version="v0",
    )

    response = client.get(f"/api/matches/{match.id_uuid}/impacts/")
    assert response.status_code == HTTPStatus.OK

    payload = response.json()
    assert payload["match_data_id"] == str(match_data.id_uuid)
    assert payload["status"] == "finished"
    assert payload["algorithm_version"] == LATEST_MATCH_IMPACT_ALGORITHM_VERSION
    assert isinstance(payload["impacts"], list)

    impacts = {row["player_id_uuid"]: row for row in payload["impacts"]}

    home_row = impacts[str(home_user.player.id_uuid)]
    away_row = impacts[str(away_user.player.id_uuid)]
    assert home_row["impact_score"] == expected_home_impact
    assert home_row["team_side"] == "home"
    assert away_row["impact_score"] == expected_away_impact
    assert away_row["team_side"] == "away"

    assert str(legacy_user.player.id_uuid) not in impacts


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_match_impacts_returns_empty_when_missing(client: Client) -> None:
    """When impacts haven't been computed yet, the endpoint returns an empty list."""
    today = timezone.now().date()
    season = Season.objects.create(name="2025", start_date=today, end_date=today)

    home_club = Club.objects.create(name="Home Club")
    away_club = Club.objects.create(name="Away Club")
    home_team = Team.objects.create(name="Home Team", club=home_club)
    away_team = Team.objects.create(name="Away Team", club=away_club)

    match = Match.objects.create(
        home_team=home_team,
        away_team=away_team,
        season=season,
        start_time=timezone.now(),
    )

    response = client.get(f"/api/matches/{match.id_uuid}/impacts/")
    assert response.status_code == HTTPStatus.OK

    payload = response.json()
    assert payload["algorithm_version"] == LATEST_MATCH_IMPACT_ALGORITHM_VERSION
    assert payload["impacts"] == []
