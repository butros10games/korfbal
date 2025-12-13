"""Tests for the connected club recent results endpoint."""

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
from apps.schedule.models import Match, Season
from apps.team.models import Team


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_connected_club_recent_results_returns_latest_three_within_days(
    client: Client,
) -> None:
    """Return latest finished matches for followed clubs within time window."""
    expected_match_count = 3
    today = timezone.now().date()
    season = Season.objects.create(
        name="2025",
        start_date=today - timedelta(days=30),
        end_date=today + timedelta(days=300),
    )

    followed_club = Club.objects.create(name="Followed Club")
    other_club = Club.objects.create(name="Other Club")

    followed_team = Team.objects.create(name="Followed Team", club=followed_club)
    other_team = Team.objects.create(name="Other Team", club=other_club)

    user = get_user_model().objects.create_user(
        username="connected_club_user",
        password="pass1234",  # noqa: S106  # nosec
    )
    player = user.player
    player.club_follow.add(followed_club)

    client.force_login(user)

    # 3 finished matches for followed club within the last 7 days.
    now = timezone.now()
    match_times = [
        now - timedelta(days=1),
        now - timedelta(days=2),
        now - timedelta(days=6),
    ]
    expected_match_ids: list[str] = []
    for start_time in match_times:
        match = Match.objects.create(
            home_team=followed_team,
            away_team=other_team,
            season=season,
            start_time=start_time,
        )
        match_data = MatchData.objects.get(match_link=match)
        match_data.status = "finished"
        match_data.save(update_fields=["status"])
        expected_match_ids.append(str(match.id_uuid))

    # A followed-club match older than the cutoff (should be excluded).
    old_match = Match.objects.create(
        home_team=followed_team,
        away_team=other_team,
        season=season,
        start_time=now - timedelta(days=20),
    )
    old_match_data = MatchData.objects.get(match_link=old_match)
    old_match_data.status = "finished"
    old_match_data.save(update_fields=["status"])

    # A recent finished match not involving the followed club (should be excluded).
    non_followed_match = Match.objects.create(
        home_team=other_team,
        away_team=other_team,
        season=season,
        start_time=now - timedelta(days=1),
    )
    non_followed_data = MatchData.objects.get(match_link=non_followed_match)
    non_followed_data.status = "finished"
    non_followed_data.save(update_fields=["status"])

    response = client.get(
        "/api/player/me/connected-clubs/recent-results/",
        data={"days": "7", "limit": "3"},
    )
    assert response.status_code == HTTPStatus.OK

    payload = response.json()
    assert isinstance(payload, list)
    assert len(payload) == expected_match_count

    returned_ids = [row["id_uuid"] for row in payload]

    # Ensure ordering: newest first
    returned_start_times = [row["start_time"] for row in payload]
    assert returned_start_times == sorted(returned_start_times, reverse=True)

    # Ensure returned are exactly the 3 recent followed matches
    assert set(returned_ids) == set(expected_match_ids)

    # Ensure payload includes expected club name for both sides
    for row in payload:
        assert row["home"]["club"] == followed_club.name
        assert row["away"]["club"] == other_club.name
