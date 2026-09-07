"""Tests for player stats API endpoints."""

from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import timedelta
from http import HTTPStatus

from django.contrib.auth import get_user_model
from django.test import override_settings
from django.test.client import Client
from django.utils import timezone
import pytest

from apps.awards.models import MatchMvp
from apps.club.models import Club
from apps.game_tracker.models import GoalType, MatchData, Shot
from apps.game_tracker.tests.tracker_test_helpers import (
    create_tracker_match,
    create_tracker_player,
)
from apps.player.services.player_overview import build_player_stats_payload
from apps.schedule.models import Match, Season
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
        password="pass1234",  # nosec
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
        password="pass1234",  # nosec
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


@pytest.mark.django_db
@pytest.mark.parametrize("populated", [False, True])
def test_player_stats_reuse_one_shot_scan(
    django_assert_num_queries: Callable[[int], AbstractContextManager[None]],
    populated: bool,
) -> None:
    """One grouped scan preserves misses, unknown types, sides and season scope."""
    tracker = create_tracker_match(prefix="Grouped player stats")
    player = create_tracker_player(username="grouped-player")
    season = tracker.match.season
    goal_type = GoalType.objects.create(name="Grouped goal")
    if populated:
        for for_team, scored, shot_type in (
            (True, True, goal_type),
            (True, False, goal_type),
            (True, True, None),
            (False, True, goal_type),
            (False, True, None),
            (False, False, None),
        ):
            Shot.objects.create(
                match_data=tracker.match_data,
                team=tracker.home_team if for_team else tracker.away_team,
                player=player,
                for_team=for_team,
                scored=scored,
                shot_type=shot_type,
            )
        history = create_tracker_match(prefix="Other player stats season")
        Shot.objects.create(
            match_data=history.match_data,
            team=history.home_team,
            player=player,
            for_team=True,
            scored=True,
        )
        Shot.objects.create(
            match_data=tracker.match_data,
            team=tracker.home_team,
            player=create_tracker_player(username="other-grouped-player"),
            for_team=True,
            scored=True,
        )

    # One MVP-ID read and one grouped shot read, including an empty season.
    with django_assert_num_queries(2):
        payload = build_player_stats_payload(player=player, season=season)

    assert {
        key: payload[key]
        for key in ("shots_for", "shots_against", "goals_for", "goals_against")
    } == {
        "shots_for": 3 if populated else 0,
        "shots_against": 3 if populated else 0,
        "goals_for": 2 if populated else 0,
        "goals_against": 2 if populated else 0,
    }
    expected = (
        {
            (str(goal_type.pk), goal_type.name, 1),
            (None, "Onbekend", 1),
        }
        if populated
        else set()
    )
    for side in ("for", "against"):
        assert {
            (row["id_uuid"], row["name"], row["count"])
            for row in payload["goal_types"][side]
        } == expected
