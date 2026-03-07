"""Tests for match impacts schedule endpoint."""

from __future__ import annotations

from decimal import Decimal
from http import HTTPStatus
import json
from pathlib import Path
from uuid import UUID

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
from apps.player.models.player import Player
from apps.schedule.models import Match, Season
from apps.team.models import Team


FIXTURES_DIR = (
    Path(__file__).resolve().parents[6] / "fixtures" / "korfbal" / "match-impact"
)


def _read_fixture(name: str) -> dict[str, object]:
    return json.loads((FIXTURES_DIR / name).read_text())


def _set_player_uuid(player: Player, player_id: str) -> Player:
    Player.objects.filter(pk=player.pk).update(id_uuid=UUID(player_id))
    return Player.objects.get(pk=player_id)


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_match_impacts_returns_persisted_rows(client: Client) -> None:
    """The endpoint should match the shared stored-impact contract fixture."""
    fixture = _read_fixture("stored-impact-contract.json")
    impacts = fixture["impacts"]
    assert isinstance(impacts, list)
    home_fixture = impacts[0]
    away_fixture = impacts[1]
    assert isinstance(home_fixture, dict)
    assert isinstance(away_fixture, dict)

    today = timezone.now().date()
    season = Season.objects.create(name="2025", start_date=today, end_date=today)

    home_club = Club.objects.create(name="Home Club")
    away_club = Club.objects.create(name="Away Club")
    home_team = Team.objects.create(
        id_uuid=UUID(str(home_fixture["team_id_uuid"])),
        name="Home Team",
        club=home_club,
    )
    away_team = Team.objects.create(
        id_uuid=UUID(str(away_fixture["team_id_uuid"])),
        name="Away Team",
        club=away_club,
    )

    match = Match.objects.create(
        id_uuid=UUID("40000000-0000-0000-0000-000000000001"),
        home_team=home_team,
        away_team=away_team,
        season=season,
        start_time=timezone.now(),
    )

    match_data = MatchData.objects.get(match_link=match)
    MatchData.objects.filter(pk=match_data.pk).update(
        id_uuid=UUID(str(fixture["match_data_id"])),
        status=str(fixture["status"]),
    )
    match_data = MatchData.objects.get(pk=fixture["match_data_id"])

    home_user = get_user_model().objects.create_user(
        username="home_player",
        password="pass1234",  # noqa: S106  # nosec
    )
    away_user = get_user_model().objects.create_user(
        username="away_player",
        password="pass1234",  # noqa: S106  # nosec
    )
    home_player = _set_player_uuid(
        home_user.player,
        str(home_fixture["player_id_uuid"]),
    )
    away_player = _set_player_uuid(
        away_user.player,
        str(away_fixture["player_id_uuid"]),
    )

    PlayerMatchImpact.objects.create(
        match_data=match_data,
        player=home_player,
        team=home_team,
        impact_score=Decimal(str(home_fixture["impact_score"])),
        algorithm_version=LATEST_MATCH_IMPACT_ALGORITHM_VERSION,
    )
    PlayerMatchImpact.objects.create(
        match_data=match_data,
        player=away_player,
        team=away_team,
        impact_score=Decimal(str(away_fixture["impact_score"])),
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
    PlayerMatchImpact.objects.filter(
        match_data=match_data,
        algorithm_version=LATEST_MATCH_IMPACT_ALGORITHM_VERSION,
    ).update(computed_at=fixture["computed_at"])

    response = client.get(f"/api/matches/{match.id_uuid}/impacts/")
    assert response.status_code == HTTPStatus.OK
    assert response.json() == fixture


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
