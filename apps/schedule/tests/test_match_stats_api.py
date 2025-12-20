"""Tests for match stats schedule endpoints."""

from http import HTTPStatus

from django.contrib.auth import get_user_model
from django.test import override_settings
from django.test.client import Client
from django.utils import timezone
import pytest

from apps.club.models import Club
from apps.game_tracker.models import (
    GoalType,
    GroupType,
    MatchData,
    MatchPlayer,
    PlayerGroup,
    Shot,
)
from apps.schedule.models import Match, Season
from apps.team.models import Team, TeamData


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_match_stats_returns_home_vs_away_counts(client: Client) -> None:  # noqa: PLR0915
    """Match stats should aggregate shots/goals per side for a match."""
    expected_shots_home = 3
    expected_shots_away = 2
    expected_goals_home = 2
    expected_goals_away = 2
    today = timezone.now().date()
    season = Season.objects.create(
        name="2025",
        start_date=today,
        end_date=today,
    )

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
    home_player = home_user.player
    away_player = away_user.player

    # A rostered player without shots should still show up.
    bench_user = get_user_model().objects.create_user(
        username="bench_player",
        password="pass1234",  # noqa: S106  # nosec
    )
    bench_player = bench_user.player
    MatchPlayer.objects.create(
        match_data=match_data,
        team=home_team,
        player=bench_player,
    )

    doorloop = GoalType.objects.create(name="Doorloop")
    vrije_bal = GoalType.objects.create(name="Vrijebal")

    # Home: 2 goals + 1 miss
    Shot.objects.create(
        match_data=match_data,
        team=home_team,
        player=home_player,
        scored=True,
        shot_type=doorloop,
    )
    Shot.objects.create(
        match_data=match_data,
        team=home_team,
        player=home_player,
        scored=True,
        shot_type=doorloop,
    )
    Shot.objects.create(
        match_data=match_data,
        team=home_team,
        player=home_player,
        scored=False,
        shot_type=doorloop,
    )

    # Away: 2 goals
    Shot.objects.create(
        match_data=match_data,
        team=away_team,
        player=away_player,
        scored=True,
        shot_type=doorloop,
    )
    Shot.objects.create(
        match_data=match_data,
        team=away_team,
        player=away_player,
        scored=True,
        shot_type=vrije_bal,
    )

    response = client.get(f"/api/matches/{match.id_uuid}/stats/")
    assert response.status_code == HTTPStatus.OK

    payload = response.json()
    assert payload["meta"]["home_team_id"] == str(home_team.id_uuid)
    assert payload["meta"]["away_team_id"] == str(away_team.id_uuid)

    general = payload["general"]
    assert general["shots_for"] == expected_shots_home
    assert general["shots_against"] == expected_shots_away
    assert general["goals_for"] == expected_goals_home
    assert general["goals_against"] == expected_goals_away

    assert general["team_goal_stats"]["Doorloop"] == {
        "goals_by_player": 2,
        "goals_against_player": 1,
    }
    assert general["team_goal_stats"]["Vrijebal"] == {
        "goals_by_player": 0,
        "goals_against_player": 1,
    }

    goal_types = {entry["name"] for entry in general["goal_types"]}
    assert {"Doorloop", "Vrijebal"}.issubset(goal_types)

    players_home = payload["players"]["home"]
    players_away = payload["players"]["away"]
    assert any(line["username"] == "home_player" for line in players_home)
    assert any(line["username"] == "away_player" for line in players_away)
    assert any(line["username"] == "bench_player" for line in players_home)

    home_line = next(line for line in players_home if line["username"] == "home_player")
    assert home_line["shots_for"] == expected_shots_home
    assert home_line["goals_for"] == expected_goals_home

    away_line = next(line for line in players_away if line["username"] == "away_player")
    assert away_line["shots_for"] == expected_shots_away
    assert away_line["goals_for"] == expected_goals_away

    bench_line = next(
        line for line in players_home if line["username"] == "bench_player"
    )
    assert bench_line["shots_for"] == 0
    assert bench_line["shots_against"] == 0
    assert bench_line["goals_for"] == 0
    assert bench_line["goals_against"] == 0


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_match_stats_prefers_roster_for_player_side(client: Client) -> None:
    """Stats should keep players on their roster side (even with mis-logged shots)."""
    today = timezone.now().date()
    season = Season.objects.create(
        name="2025",
        start_date=today,
        end_date=today,
    )

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
    home_player = home_user.player
    away_player = away_user.player

    MatchPlayer.objects.create(
        match_data=match_data,
        team=home_team,
        player=home_player,
    )
    MatchPlayer.objects.create(
        match_data=match_data,
        team=away_team,
        player=away_player,
    )

    doorloop = GoalType.objects.create(name="Doorloop")

    # Simulate a mis-logged goal: shot counted for away team, but linked to a
    # home team player.
    Shot.objects.create(
        match_data=match_data,
        team=away_team,
        player=home_player,
        scored=True,
        shot_type=doorloop,
    )

    response = client.get(f"/api/matches/{match.id_uuid}/stats/")
    assert response.status_code == HTTPStatus.OK

    payload = response.json()
    players_home = payload["players"]["home"]
    players_away = payload["players"]["away"]

    assert any(line["username"] == "home_player" for line in players_home)
    assert not any(line["username"] == "home_player" for line in players_away)


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_match_stats_prefers_teamdata_for_player_side_when_roster_missing(
    client: Client,
) -> None:
    """Stats should keep players on their TeamData side when roster is missing."""
    today = timezone.now().date()
    season = Season.objects.create(
        name="2025",
        start_date=today,
        end_date=today,
    )

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
    home_player = home_user.player
    away_player = away_user.player

    # No MatchPlayer rows for this match; fall back to TeamData membership.
    home_team_data = TeamData.objects.create(team=home_team, season=season)
    away_team_data = TeamData.objects.create(team=away_team, season=season)
    home_team_data.players.add(home_player)
    away_team_data.players.add(away_player)

    doorloop = GoalType.objects.create(name="Doorloop")

    # Mis-logged shot: registered for away team, but linked to a home team player.
    Shot.objects.create(
        match_data=match_data,
        team=away_team,
        player=home_player,
        scored=True,
        shot_type=doorloop,
    )

    # A normal away shot for control.
    Shot.objects.create(
        match_data=match_data,
        team=away_team,
        player=away_player,
        scored=False,
        shot_type=doorloop,
    )

    response = client.get(f"/api/matches/{match.id_uuid}/stats/")
    assert response.status_code == HTTPStatus.OK

    payload = response.json()
    players_home = payload["players"]["home"]
    players_away = payload["players"]["away"]

    assert any(line["username"] == "home_player" for line in players_home)
    assert not any(line["username"] == "home_player" for line in players_away)


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_match_stats_prefers_playergroup_for_player_side_when_roster_missing(
    client: Client,
) -> None:
    """Stats should keep players on their PlayerGroup side when roster/TeamData are
    missing.

    This covers the real-world scenario where a guest player is assigned to a team
    via match tracking (PlayerGroup), but some of their shots were accidentally
    logged under the opponent team.
    """
    today = timezone.now().date()
    season = Season.objects.create(
        name="2025",
        start_date=today,
        end_date=today,
    )

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

    # Player is not in TeamData and there are no MatchPlayer rows.
    guest_user = get_user_model().objects.create_user(
        username="guest_player",
        password="pass1234",  # noqa: S106  # nosec
    )
    guest_player = guest_user.player

    reserve_type = GroupType.objects.create(name="Reserve", order=0)
    group = PlayerGroup.objects.create(
        match_data=match_data,
        team=home_team,
        starting_type=reserve_type,
        current_type=reserve_type,
    )
    group.players.add(guest_player)

    doorloop = GoalType.objects.create(name="Doorloop")

    # Shots are skewed to the away team due to mis-logging.
    Shot.objects.create(
        match_data=match_data,
        team=home_team,
        player=guest_player,
        scored=False,
        shot_type=doorloop,
    )
    Shot.objects.create(
        match_data=match_data,
        team=home_team,
        player=guest_player,
        scored=True,
        shot_type=doorloop,
    )
    for _ in range(4):
        Shot.objects.create(
            match_data=match_data,
            team=away_team,
            player=guest_player,
            scored=False,
            shot_type=doorloop,
        )

    response = client.get(f"/api/matches/{match.id_uuid}/stats/")
    assert response.status_code == HTTPStatus.OK

    payload = response.json()
    players_home = payload["players"]["home"]
    players_away = payload["players"]["away"]

    assert any(line["username"] == "guest_player" for line in players_home)
    assert not any(line["username"] == "guest_player" for line in players_away)
