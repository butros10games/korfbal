"""Tests for player stats API endpoints."""

from datetime import timedelta
from http import HTTPStatus

from django.contrib.auth import get_user_model
from django.test import override_settings
from django.test.client import Client
from django.utils import timezone
import pytest

from apps.club.models import Club
from apps.game_tracker.models import GoalType, MatchData, Shot
from apps.schedule.models import Match, Season
from apps.schedule.models.mvp import MatchMvp
from apps.team.models import Team


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_player_stats_returns_counts_for_season(client: Client) -> None:
    """Player stats endpoint should aggregate shot/goal counts for a season."""
    today = timezone.now().date()
    season = Season.objects.create(
        name="2025",
        start_date=today - timedelta(days=30),
        end_date=today + timedelta(days=300),
    )
    previous_season = Season.objects.create(
        name="2024",
        start_date=today - timedelta(days=400),
        end_date=today - timedelta(days=200),
    )

    club = Club.objects.create(name="Stats Club")
    opponent_club = Club.objects.create(name="Opp Club")
    team = Team.objects.create(name="Team A", club=club)
    opponent_team = Team.objects.create(name="Team B", club=opponent_club)

    user = get_user_model().objects.create_user(
        username="stat_player",
        password="pass1234",  # noqa: S106  # nosec
    )
    player = user.player

    match = Match.objects.create(
        home_team=team,
        away_team=opponent_team,
        season=season,
        start_time=timezone.now(),
    )
    match_data = MatchData.objects.get(match_link=match)
    match_data.status = "finished"
    match_data.save(update_fields=["status"])

    goal_type_for = GoalType.objects.create(name="Doorloop")
    goal_type_against = GoalType.objects.create(name="Vrijebal")

    Shot.objects.create(
        match_data=match_data,
        player=player,
        team=team,
        for_team=True,
        scored=True,
        shot_type=goal_type_for,
    )
    Shot.objects.create(
        match_data=match_data,
        player=player,
        team=team,
        for_team=True,
        scored=False,
        shot_type=goal_type_for,
    )
    Shot.objects.create(
        match_data=match_data,
        player=player,
        team=opponent_team,
        for_team=False,
        scored=True,
        shot_type=goal_type_against,
    )

    # Add a shot in a different season to ensure filtering works
    legacy_match = Match.objects.create(
        home_team=team,
        away_team=opponent_team,
        season=previous_season,
        start_time=timezone.now() - timedelta(days=300),
    )
    legacy_match_data = MatchData.objects.get(match_link=legacy_match)
    Shot.objects.create(
        match_data=legacy_match_data,
        player=player,
        team=team,
        for_team=True,
        scored=True,
        shot_type=goal_type_for,
    )

    response = client.get(f"/api/player/players/{player.id_uuid}/stats/")
    assert response.status_code == HTTPStatus.OK
    payload = response.json()
    assert payload == {
        "shots_for": 2,
        "shots_against": 1,
        "goals_for": 1,
        "goals_against": 1,
        "mvps": 0,
        "mvp_matches": [],
        "goal_types": {
            "for": [
                {
                    "id_uuid": str(goal_type_for.id_uuid),
                    "name": goal_type_for.name,
                    "count": 1,
                },
            ],
            "against": [
                {
                    "id_uuid": str(goal_type_against.id_uuid),
                    "name": goal_type_against.name,
                    "count": 1,
                },
            ],
        },
    }

    # Filter explicitly to the previous season
    response_prev = client.get(
        f"/api/player/players/{player.id_uuid}/stats/",
        data={"season": previous_season.id_uuid},
    )
    assert response_prev.status_code == HTTPStatus.OK
    payload_prev = response_prev.json()
    assert payload_prev == {
        "shots_for": 1,
        "shots_against": 0,
        "goals_for": 1,
        "goals_against": 0,
        "mvps": 0,
        "mvp_matches": [],
        "goal_types": {
            "for": [
                {
                    "id_uuid": str(goal_type_for.id_uuid),
                    "name": goal_type_for.name,
                    "count": 1,
                },
            ],
            "against": [],
        },
    }


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_player_stats_includes_mvps_and_match_summaries(client: Client) -> None:
    """Player stats endpoint should include MVP counts and match summaries.

    Args:
        client (Client): Django test client.

    """
    today = timezone.now().date()
    season = Season.objects.create(
        name="2025",
        start_date=today - timedelta(days=30),
        end_date=today + timedelta(days=300),
    )

    club = Club.objects.create(name="Stats Club")
    opponent_club = Club.objects.create(name="Opp Club")
    team = Team.objects.create(name="Team A", club=club)
    opponent_team = Team.objects.create(name="Team B", club=opponent_club)

    user = get_user_model().objects.create_user(
        username="mvp_player",
        password="pass1234",  # noqa: S106  # nosec
    )
    player = user.player

    match = Match.objects.create(
        home_team=team,
        away_team=opponent_team,
        season=season,
        start_time=timezone.now(),
    )
    match_data = MatchData.objects.get(match_link=match)
    match_data.status = "finished"
    match_data.save(update_fields=["status"])

    MatchMvp.objects.create(
        match=match,
        finished_at=timezone.now(),
        closes_at=timezone.now() + timedelta(hours=3),
        mvp_player=player,
        published_at=timezone.now(),
    )

    response = client.get(f"/api/player/players/{player.id_uuid}/stats/")
    assert response.status_code == HTTPStatus.OK
    payload = response.json()

    assert payload["mvps"] == 1
    assert isinstance(payload.get("mvp_matches"), list)
    assert len(payload["mvp_matches"]) == 1
    summary = payload["mvp_matches"][0]
    assert summary["id_uuid"] == str(match.id_uuid)
    assert summary["match_data_id"] == str(match_data.id_uuid)
