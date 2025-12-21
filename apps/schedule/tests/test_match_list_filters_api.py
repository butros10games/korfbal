"""Tests for match list and filtering endpoints.

These endpoints are heavily used by the frontend and are easy to regress because they
combine time-window logic with query-param filtering.

"""

from __future__ import annotations

from datetime import timedelta
from http import HTTPStatus

from django.contrib.auth import get_user_model
from django.test import override_settings
from django.test.client import Client
from django.utils import timezone
import pytest

from apps.club.models import Club
from apps.game_tracker.models import MatchData
from apps.player.models import Player
from apps.schedule.models import Match, Season
from apps.team.models import Team


DEFAULT_UPCOMING_LIMIT = 5
MINIMUM_LIMIT = 1


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_match_next_scopes_to_followed_teams(client: Client) -> None:
    """The next endpoint should respect ?followed=1 for authenticated players."""
    today = timezone.now().date()
    season = Season.objects.create(name="2025", start_date=today, end_date=today)

    club = Club.objects.create(name="Club A")
    other_club = Club.objects.create(name="Club B")

    followed_team = Team.objects.create(name="Followed", club=club)
    other_team = Team.objects.create(name="Other", club=other_club)
    unrelated_team = Team.objects.create(name="Unrelated", club=other_club)

    now = timezone.now()

    # This match is earlier, but does not involve the followed team.
    Match.objects.create(
        home_team=other_team,
        away_team=unrelated_team,
        season=season,
        start_time=now + timedelta(hours=1),
    )

    followed_match = Match.objects.create(
        home_team=followed_team,
        away_team=other_team,
        season=season,
        start_time=now + timedelta(hours=2),
    )

    for match in Match.objects.all():
        data = MatchData.objects.get(match_link=match)
        data.status = "upcoming"
        data.save(update_fields=["status"])

    user = get_user_model().objects.create_user(
        username="viewer",
        password="pass1234",  # noqa: S106  # nosec
    )
    user.player.team_follow.add(followed_team)

    client.force_login(user)
    response = client.get("/api/matches/next/", {"followed": "1"})

    assert response.status_code == HTTPStatus.OK
    payload = response.json()
    assert payload["id_uuid"] == str(followed_match.id_uuid)


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_match_followed_does_not_crash_when_user_has_no_player(client: Client) -> None:
    """Regression test: followed endpoints should tolerate users without Player rows."""
    today = timezone.now().date()
    season = Season.objects.create(name="2025", start_date=today, end_date=today)

    club = Club.objects.create(name="Club")
    opponent_club = Club.objects.create(name="Opponent")
    team = Team.objects.create(name="Team", club=club)
    opponent = Team.objects.create(name="Opponent", club=opponent_club)

    match = Match.objects.create(
        home_team=team,
        away_team=opponent,
        season=season,
        start_time=timezone.now() + timedelta(days=1),
    )
    match_data = MatchData.objects.get(match_link=match)
    match_data.status = "upcoming"
    match_data.save(update_fields=["status"])

    user = get_user_model().objects.create_user(
        username="no_player",
        password="pass1234",  # noqa: S106  # nosec
    )
    # Delete the auto-created player row (signal) to mimic a partially-migrated user.
    Player.objects.filter(user=user).delete()

    client.force_login(user)
    response = client.get("/api/matches/upcoming/", {"followed": "1"})

    assert response.status_code == HTTPStatus.OK
    payload = response.json()
    assert isinstance(payload, list)
    assert payload, "Expected at least one upcoming match"


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_match_upcoming_filters_by_team_and_limit_parsing(client: Client) -> None:
    """Upcoming should filter by team and handle bad/zero limits safely."""
    today = timezone.now().date()
    season = Season.objects.create(name="2025", start_date=today, end_date=today)

    club = Club.objects.create(name="Club")
    opponent_club = Club.objects.create(name="Opponent")

    team = Team.objects.create(name="Team", club=club)
    opponent = Team.objects.create(name="Opponent", club=opponent_club)

    now = timezone.now()

    for i in range(6):
        match = Match.objects.create(
            home_team=team,
            away_team=opponent,
            season=season,
            start_time=now + timedelta(hours=i + 1),
        )
        data = MatchData.objects.get(match_link=match)
        data.status = "upcoming"
        data.save(update_fields=["status"])

    other_club = Club.objects.create(name="Other")
    other_team = Team.objects.create(name="Other Team", club=other_club)
    other_match = Match.objects.create(
        home_team=other_team,
        away_team=opponent,
        season=season,
        start_time=now + timedelta(hours=1),
    )
    other_data = MatchData.objects.get(match_link=other_match)
    other_data.status = "upcoming"
    other_data.save(update_fields=["status"])

    response_default_limit = client.get(
        "/api/matches/upcoming/",
        {"team": str(team.id_uuid), "limit": "not-an-int"},
    )
    assert response_default_limit.status_code == HTTPStatus.OK
    payload = response_default_limit.json()
    assert len(payload) == DEFAULT_UPCOMING_LIMIT
    assert all(
        line["home_team"]["id_uuid"] == str(team.id_uuid)
        or line["away_team"]["id_uuid"] == str(team.id_uuid)
        for line in payload
    )

    response_zero_limit = client.get(
        "/api/matches/upcoming/",
        {"team": str(team.id_uuid), "limit": "0"},
    )
    assert response_zero_limit.status_code == HTTPStatus.OK
    assert len(response_zero_limit.json()) == MINIMUM_LIMIT


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_match_recent_uses_seven_day_window(client: Client) -> None:
    """Recent should only include matches started in the last 7 days."""
    today = timezone.now().date()
    season = Season.objects.create(name="2025", start_date=today, end_date=today)

    club = Club.objects.create(name="Club")
    opponent_club = Club.objects.create(name="Opponent")

    team = Team.objects.create(name="Team", club=club)
    opponent = Team.objects.create(name="Opponent", club=opponent_club)

    within_window = Match.objects.create(
        home_team=team,
        away_team=opponent,
        season=season,
        start_time=timezone.now() - timedelta(days=3),
    )
    outside_window = Match.objects.create(
        home_team=team,
        away_team=opponent,
        season=season,
        start_time=timezone.now() - timedelta(days=10),
    )

    for match in (within_window, outside_window):
        data = MatchData.objects.get(match_link=match)
        data.status = "finished"
        data.home_score = 10
        data.away_score = 9
        data.save(update_fields=["status", "home_score", "away_score"])

    response = client.get("/api/matches/recent/")
    assert response.status_code == HTTPStatus.OK
    payload = response.json()

    ids = {row["id_uuid"] for row in payload}
    assert str(within_window.id_uuid) in ids
    assert str(outside_window.id_uuid) not in ids


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_match_finished_respects_team_and_season_filters(client: Client) -> None:
    """Finished summaries should respect team + season filtering."""
    today = timezone.now().date()
    season = Season.objects.create(name="2025", start_date=today, end_date=today)
    other_season = Season.objects.create(
        name="2024",
        start_date=today - timedelta(days=400),
        end_date=today - timedelta(days=200),
    )

    club = Club.objects.create(name="Club")
    opponent_club = Club.objects.create(name="Opponent")

    team = Team.objects.create(name="Team", club=club)
    opponent = Team.objects.create(name="Opponent", club=opponent_club)

    match_in_scope = Match.objects.create(
        home_team=team,
        away_team=opponent,
        season=season,
        start_time=timezone.now() - timedelta(days=1),
    )
    match_outside_season = Match.objects.create(
        home_team=team,
        away_team=opponent,
        season=other_season,
        start_time=timezone.now() - timedelta(days=300),
    )

    for match in (match_in_scope, match_outside_season):
        data = MatchData.objects.get(match_link=match)
        data.status = "finished"
        data.home_score = 21
        data.away_score = 18
        data.save(update_fields=["status", "home_score", "away_score"])

    response = client.get(
        "/api/matches/finished/",
        {"team": str(team.id_uuid), "season": str(season.id_uuid), "limit": "5"},
    )

    assert response.status_code == HTTPStatus.OK
    payload = response.json()

    assert isinstance(payload, list)
    assert payload
    assert all("score" in item for item in payload)

    ids = {item["id_uuid"] for item in payload}
    assert str(match_in_scope.id_uuid) in ids
    assert str(match_outside_season.id_uuid) not in ids
