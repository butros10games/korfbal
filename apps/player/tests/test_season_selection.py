"""Explicit player season scopes and running-team discovery (season audit)."""

from __future__ import annotations

from datetime import date, timedelta
from http import HTTPStatus
from uuid import UUID

from django.contrib.auth import get_user_model
from django.utils import timezone
import pytest
from rest_framework.test import APIClient

from apps.club.models import Club
from apps.game_tracker.models import MatchData, Shot
from apps.player.models import Player
from apps.player.services.player_teams import (
    connected_team_ids,
    grouped_teams_for_player,
)
from apps.schedule.models import Match, Season
from apps.schedule.queries.seasons import current_season
from apps.team.models import Team, TeamData


pytestmark = pytest.mark.django_db
# A well-formed season ID that belongs to no player.
FOREIGN_SEASON = UUID("01900000-0000-7000-8000-000000000001")


def sides() -> tuple[Team, Team]:
    """Two native teams of different clubs."""
    home = Team.objects.create(
        name="Synthetic 1", club=Club.objects.create(name="Home")
    )
    away = Team.objects.create(
        name="Synthetic 1", club=Club.objects.create(name="Away")
    )
    return home, away


def goals_player() -> tuple[APIClient, Player, list[Season]]:
    """Create a player with one goal in each of two seasons."""
    user = get_user_model().objects.create_user(username="synthetic-season-player")
    home, away = sides()
    seasons = [
        Season.objects.create(
            name=f"Selection {year}",
            start_date=date(year, 7, 1),
            end_date=date(year + 1, 6, 30),
        )
        for year in (2024, 2025)
    ]
    for season in seasons:
        TeamData.objects.create(team=home, season=season).players.add(user.player)
        match = Match.objects.create(
            home_team=home,
            away_team=away,
            season=season,
            start_time=timezone.make_aware(
                timezone.datetime(season.start_date.year, 9, 5, 14)
            ),
        )
        tracker = MatchData.objects.get(match_link=match)
        MatchData.objects.filter(pk=tracker.pk).update(status="finished")
        Shot.objects.create(
            match_data=tracker,
            player=user.player,
            team=home,
            scored=True,
        )
    client = APIClient()
    client.force_authenticate(user)
    return client, user.player, seasons


@pytest.mark.parametrize("route", ["stats", "overview"])
@pytest.mark.parametrize("value", [str(FOREIGN_SEASON), "not-a-uuid", "  "])
def test_invalid_explicit_season_is_a_client_error(route: str, value: str) -> None:
    """A stale or foreign season never silently widens to career totals."""
    client, player, _ = goals_player()
    response = client.get(
        f"/api/player/players/{player.pk}/{route}/", {"season": value}
    )
    if not value.strip():
        # Blank is an omitted selection, resolved to the documented default.
        assert response.status_code == HTTPStatus.OK
        return
    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert response.json()["code"] == "invalid_season"


def test_explicit_career_and_season_scopes() -> None:
    """Only an explicit 'career' request combines seasons."""
    client, player, seasons = goals_player()
    route = f"/api/player/players/{player.pk}/stats/"
    one = client.get(route, {"season": str(seasons[0].pk)}).json()
    career = client.get(route, {"season": "career"}).json()
    default = client.get(route).json()
    assert one["goals_for"] == 1
    assert one["meta"]["season_scope"] == "explicit"
    assert career["goals_for"] == 2  # noqa: PLR2004 - both seasons
    assert career["meta"] == {
        "season_id": None,
        "season_name": None,
        "season_scope": "career",
    }
    assert default["goals_for"] == 1
    assert default["meta"]["season_scope"] == "default"
    overview = client.get(
        f"/api/player/players/{player.pk}/overview/", {"season": "career"}
    ).json()
    assert overview["meta"]["season_scope"] == "career"
    assert len(overview["matches"]["recent"]) == 2  # noqa: PLR2004


def test_overlapping_competitions_keep_every_running_team() -> None:
    """An unrelated catalogue fixture cannot hide the player's own team."""
    today = timezone.localdate()
    now = timezone.now()
    outdoor = Season.objects.create(
        name="Outdoor running",
        start_date=today - timedelta(days=90),
        end_date=today + timedelta(days=90),
    )
    indoor = Season.objects.create(
        name="Indoor running",
        start_date=today - timedelta(days=1),
        end_date=today + timedelta(days=90),
    )
    home, away = sides()
    indoor_team = Team.objects.create(name="Synthetic Zaal", club=home.club)
    player = Player.objects.create(name="Synthetic both")
    coach = Player.objects.create(name="Synthetic coach")
    TeamData.objects.create(team=home, season=outdoor).players.add(player)
    TeamData.objects.create(team=indoor_team, season=indoor).players.add(player)
    TeamData.objects.get(team=home, season=outdoor).coach.add(coach)
    other_home = Team.objects.create(name="Synthetic 2", club=away.club)
    other_away = Team.objects.create(
        name="Synthetic 3", club=Club.objects.create(name="Elsewhere")
    )
    Match.objects.create(
        home_team=home,
        away_team=away,
        season=outdoor,
        start_time=now + timedelta(days=2),
    )
    Match.objects.create(
        home_team=other_home,
        away_team=other_away,
        season=indoor,
        start_time=now + timedelta(days=1),
    )
    # The catalogue-wide choice still prefers the earliest fixture's season...
    assert current_season() == indoor
    # ...but the player's own running teams do not depend on it.
    assert {home.pk, indoor_team.pk} <= set(connected_team_ids(player))
    groups = grouped_teams_for_player(player)
    assert set(groups.playing.values_list("pk", flat=True)) == {
        home.pk,
        indoor_team.pk,
    }
    assert not groups.coaching.exists()
    coach_groups = grouped_teams_for_player(coach)
    assert list(coach_groups.coaching.values_list("pk", flat=True)) == [home.pk]
    assert not coach_groups.playing.exists()


def test_finished_seasons_are_not_running_teams() -> None:
    """Historical rosters stay history; they are not 'my teams' today."""
    home, _ = sides()
    player = Player.objects.create(name="Synthetic former")
    old = Season.objects.create(
        name="Finished", start_date=date(2020, 7, 1), end_date=date(2021, 6, 30)
    )
    TeamData.objects.create(team=home, season=old).players.add(player)
    assert connected_team_ids(player) == []
    assert not grouped_teams_for_player(player).playing.exists()
