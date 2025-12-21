"""Tests for the player overview API.

The overview endpoints power player profile screens. They combine:
- privacy/visibility rules
- season selection
- match participation vs roster inclusion

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
from apps.game_tracker.models import MatchData, Shot
from apps.player.models import Player, PlayerClubMembership
from apps.schedule.models import Match, Season
from apps.team.models import Team, TeamData


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_player_overview_me_includes_upcoming_roster_and_recent_participation(
    client: Client,
) -> None:
    """Upcoming includes roster matches; recent requires actual participation."""
    today = timezone.now().date()
    season = Season.objects.create(
        name="2025",
        start_date=today - timedelta(days=5),
        end_date=today + timedelta(days=200),
    )

    club = Club.objects.create(name="Club")
    opp_club = Club.objects.create(name="Opponent")

    team = Team.objects.create(name="Team", club=club)
    opp_team = Team.objects.create(name="Opp", club=opp_club)

    user = get_user_model().objects.create_user(
        username="player",
        password="pass1234",  # noqa: S106  # nosec
    )
    player = user.player

    # Roster membership should be enough for the upcoming list.
    team_data = TeamData.objects.create(team=team, season=season)
    team_data.players.add(player)

    upcoming_match = Match.objects.create(
        home_team=team,
        away_team=opp_team,
        season=season,
        start_time=timezone.now() + timedelta(days=1),
    )
    upcoming_data = MatchData.objects.get(match_link=upcoming_match)
    upcoming_data.status = "upcoming"
    upcoming_data.save(update_fields=["status"])

    finished_with_shot = Match.objects.create(
        home_team=team,
        away_team=opp_team,
        season=season,
        start_time=timezone.now() - timedelta(days=1),
    )
    finished_data = MatchData.objects.get(match_link=finished_with_shot)
    finished_data.status = "finished"
    finished_data.home_score = 10
    finished_data.away_score = 9
    finished_data.save(update_fields=["status", "home_score", "away_score"])

    # Recent list only includes participation (shots or player groups).
    Shot.objects.create(match_data=finished_data, player=player, team=team, scored=True)

    finished_no_participation = Match.objects.create(
        home_team=team,
        away_team=opp_team,
        season=season,
        start_time=timezone.now() - timedelta(days=2),
    )
    finished_no_participation_data = MatchData.objects.get(
        match_link=finished_no_participation
    )
    finished_no_participation_data.status = "finished"
    finished_no_participation_data.home_score = 5
    finished_no_participation_data.away_score = 4
    finished_no_participation_data.save(
        update_fields=["status", "home_score", "away_score"]
    )

    client.force_login(user)

    response = client.get("/api/player/me/overview/")
    assert response.status_code == HTTPStatus.OK

    payload = response.json()

    upcoming_ids = {m["id_uuid"] for m in payload["matches"]["upcoming"]}
    recent_ids = {m["id_uuid"] for m in payload["matches"]["recent"]}

    assert str(upcoming_match.id_uuid) in upcoming_ids
    assert str(finished_with_shot.id_uuid) in recent_ids
    assert str(finished_no_participation.id_uuid) not in recent_ids

    assert payload["meta"]["season_id"] == str(season.id_uuid)
    assert payload["meta"]["season_name"] == season.name
    assert payload["seasons"], "Expected season options to be returned"


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_player_overview_respects_visibility_for_other_viewers(client: Client) -> None:
    """Club/private visibility should block unconnected viewers; allow connected."""
    today = timezone.now().date()
    season = Season.objects.create(
        name="2025",
        start_date=today - timedelta(days=30),
        end_date=today + timedelta(days=300),
    )

    club = Club.objects.create(name="Target Club")
    opp_club = Club.objects.create(name="Opponent")
    team = Team.objects.create(name="Team", club=club)
    opp_team = Team.objects.create(name="Opp", club=opp_club)

    target_user = get_user_model().objects.create_user(
        username="target",
        password="pass1234",  # noqa: S106  # nosec
    )
    target = target_user.player

    # Make club membership explicit so privacy checks use PlayerClubMembership.
    PlayerClubMembership.objects.create(
        player=target,
        club=club,
        start_date=today,
        end_date=None,
    )

    target.stats_visibility = Player.Visibility.PRIVATE
    target.save(update_fields=["stats_visibility"])

    team_data = TeamData.objects.create(team=team, season=season)
    team_data.players.add(target)

    match = Match.objects.create(
        home_team=team,
        away_team=opp_team,
        season=season,
        start_time=timezone.now() + timedelta(days=1),
    )
    data = MatchData.objects.get(match_link=match)
    data.status = "upcoming"
    data.save(update_fields=["status"])

    viewer_user = get_user_model().objects.create_user(
        username="viewer",
        password="pass1234",  # noqa: S106  # nosec
    )

    client.force_login(viewer_user)

    response_forbidden = client.get(f"/api/player/players/{target.id_uuid}/overview/")
    assert response_forbidden.status_code == HTTPStatus.FORBIDDEN

    # Connect viewer to the same club -> should be allowed.
    PlayerClubMembership.objects.create(
        player=viewer_user.player,
        club=club,
        start_date=today,
        end_date=None,
    )

    response_ok = client.get(f"/api/player/players/{target.id_uuid}/overview/")
    assert response_ok.status_code == HTTPStatus.OK

    ok_payload = response_ok.json()
    assert ok_payload["meta"]["season_id"] == str(season.id_uuid)
    assert ok_payload["matches"]["upcoming"], "Expected upcoming matches for target"
