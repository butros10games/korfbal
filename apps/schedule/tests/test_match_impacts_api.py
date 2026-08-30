"""Tests for match impacts schedule endpoint."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from http import HTTPStatus
import json
from pathlib import Path
from uuid import UUID

from django.contrib.auth import get_user_model
from django.test.client import Client
from django.utils import timezone
import pytest

from apps.club.models import Club
from apps.game_tracker.models import (
    MatchData,
    MatchPart,
    PlayerMatchImpact,
    PossessionChange,
)
from apps.game_tracker.services.match_impact import (
    LATEST_MATCH_IMPACT_ALGORITHM_VERSION,
)
from apps.player.models.player import Player
from apps.schedule.models import Match, Season
from apps.team.models import Team


FIXTURES_DIR = (
    Path(__file__).resolve().parents[6] / "fixtures" / "korfbal" / "match-impact"
)
pytestmark = pytest.mark.django_db


def _read_fixture(name: str) -> dict[str, object]:
    return json.loads((FIXTURES_DIR / name).read_text())


def _set_player_uuid(player: Player, player_id: str) -> Player:
    Player.objects.filter(pk=player.pk).update(id_uuid=UUID(player_id))
    return Player.objects.get(pk=player_id)


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
    )
    away_user = get_user_model().objects.create_user(
        username="away_player",
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
        win_probability_added=Decimal(str(home_fixture["win_probability_added"])),
        algorithm_version=LATEST_MATCH_IMPACT_ALGORITHM_VERSION,
    )
    PlayerMatchImpact.objects.create(
        match_data=match_data,
        player=away_player,
        team=away_team,
        impact_score=Decimal(str(away_fixture["impact_score"])),
        win_probability_added=Decimal(str(away_fixture["win_probability_added"])),
        algorithm_version=LATEST_MATCH_IMPACT_ALGORITHM_VERSION,
    )
    # Noise row at older version should not be returned.
    legacy_user = get_user_model().objects.create_user(
        username="legacy_player",
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


def test_match_impacts_exposes_possession_contribution(client: Client) -> None:
    """The canonical response explains which player's interception added value."""
    today = timezone.now().date()
    season = Season.objects.create(name="2026", start_date=today, end_date=today)
    home_team = Team.objects.create(
        name="Home Team",
        club=Club.objects.create(name="Home Club"),
    )
    away_team = Team.objects.create(
        name="Away Team",
        club=Club.objects.create(name="Away Club"),
    )
    start = timezone.now() - timedelta(minutes=20)
    match = Match.objects.create(
        home_team=home_team,
        away_team=away_team,
        season=season,
        start_time=start,
    )
    match_data = MatchData.objects.get(match_link=match)
    match_data.status = "finished"
    match_data.save(update_fields=["status"])
    part = MatchPart.objects.create(
        match_data=match_data,
        part_number=1,
        start_time=start,
        active=False,
    )
    player = get_user_model().objects.create_user(username="interceptor").player
    PossessionChange.objects.create(
        match_data=match_data,
        match_part=part,
        team=home_team,
        player=player,
        kind=PossessionChange.INTERCEPTION,
        time=start + timedelta(minutes=5),
    )

    response = client.get(f"/api/matches/{match.id_uuid}/impacts/")

    assert response.status_code == HTTPStatus.OK
    payload = response.json()
    assert payload["algorithm_version"] == "v8"
    assert payload["score_unit"] == "expected_goal_value_added"
    assert payload["wpa_unit"] == "win_expectancy_added"
    assert payload["win_probability_model"] == "poisson-possession-v1"
    impact = next(
        row
        for row in payload["impacts"]
        if row["player_id_uuid"] == str(player.id_uuid)
    )
    assert impact["impact_score"] == pytest.approx(0.18)
    assert impact["win_probability_added"] > 0
    assert impact["team_id_uuid"] == str(home_team.id_uuid)
    assert len(impact["contributions"]) == 1
    contribution = impact["contributions"][0]
    assert contribution["player_id"] == str(player.id_uuid)
    assert contribution["time"] == "5"
    assert contribution["category"] == "possession_gain"
    assert contribution["points"] == pytest.approx(0.18)
    assert contribution["source_type"] == "possession_change"
    assert contribution["possession_kind"] == "interception"
    assert contribution["base_points"] == pytest.approx(0.18)
    assert contribution["leverage_multiplier"] == pytest.approx(1.0)
    assert contribution["transition_bonus"] == pytest.approx(0.0)
    assert contribution["linked_goal_event_id"] is None
    assert contribution["source_event_id"]
    assert contribution["team_id"] == str(home_team.id_uuid)
    assert contribution["win_probability_added"] > 0
    assert contribution["win_expectancy_after"] > contribution["win_expectancy_before"]
