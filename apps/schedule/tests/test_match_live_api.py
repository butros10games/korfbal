"""Tests for match live state schedule endpoints."""

from http import HTTPStatus

from django.contrib.auth import get_user_model
from django.test import override_settings
from django.test.client import Client
from django.utils import timezone
import pytest

from apps.club.models import Club
from apps.game_tracker.models import GoalType, MatchData, Shot
from apps.schedule.models import Match, Season
from apps.team.models import Team


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_match_live_state_is_public(client: Client) -> None:
    """Live state should be available without authentication."""
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

    response = client.get(f"/api/matches/{match.id_uuid}/live/")
    assert response.status_code == HTTPStatus.OK


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_match_live_state_and_poll_return_payload(client: Client) -> None:
    """Live state should return timer/score and poll should timeout and then change."""
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
    match_data.status = "active"
    match_data.save(update_fields=["status"])

    user = get_user_model().objects.create_user(
        username="viewer",
        password="pass1234",  # noqa: S106  # nosec
    )

    response = client.get(f"/api/matches/{match.id_uuid}/live/")
    assert response.status_code == HTTPStatus.OK

    payload = response.json()
    assert payload["match_id"] == str(match.id_uuid)
    assert payload["match_data_id"] == str(match_data.id_uuid)
    assert payload["status"] == "active"
    assert "timer" in payload
    assert payload["score"] == {"home": 0, "away": 0}
    assert payload["last_changed_at"]

    # No change: should return a timeout payload.
    response = client.get(
        f"/api/matches/{match.id_uuid}/live/poll/",
        {"since": payload["last_changed_at"], "timeout": "1"},
    )
    assert response.status_code == HTTPStatus.OK
    poll_payload = response.json()
    assert poll_payload["changed"] is False
    assert poll_payload["last_changed_at"]

    # Change: register a goal for home; poll should return an updated snapshot.
    goal_type = GoalType.objects.create(name="Doorloop")
    Shot.objects.create(
        match_data=match_data,
        team=home_team,
        player=user.player,
        time=timezone.now(),
        scored=True,
        shot_type=goal_type,
    )

    response = client.get(
        f"/api/matches/{match.id_uuid}/live/poll/",
        {"since": payload["last_changed_at"], "timeout": "1"},
    )
    assert response.status_code == HTTPStatus.OK
    updated = response.json()

    assert "changed" not in updated
    assert updated["score"] == {"home": 1, "away": 0}
