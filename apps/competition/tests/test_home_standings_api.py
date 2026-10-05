"""Tests for the Home poule standings of a player's teams."""

from __future__ import annotations

from datetime import timedelta
from http import HTTPStatus

from django.contrib.auth import get_user_model
from django.db import connection
from django.test.client import Client
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
import pytest

from apps.club.models import Club
from apps.competition.models import (
    Club as SourceClub,
    Match as SourceMatch,
    Pool,
    PoolEntry,
    Team as SourceTeam,
    TeamGroup,
)
from apps.competition.services.home_standings import MAX_HOME_TEAMS
from apps.game_tracker.models import MatchData
from apps.schedule.models import Match, Season, SeasonPool
from apps.team.models import Team


pytestmark = pytest.mark.django_db
URL = "/api/competition/pools/home-standings/"


class Poule:
    """One running-season poule with native teams linked to source entries."""

    def __init__(
        self, season: Season, name: str, native_season: Season | None = None
    ) -> None:
        """Create the poule in both the schedule and the competition catalogue."""
        self.season = season
        self.native_season = native_season or season
        self.local = SeasonPool.objects.create(season=self.native_season, name=name)
        self.pool = Pool.objects.create(
            season=season,
            external_id=name,
            name=name,
            class_name="Senioren 1e klasse",
            results_filtered=False,
            local_pool=self.local,
        )
        self.source_club = SourceClub.objects.get_or_create(
            external_id="source", defaults={"name": "Source"}
        )[0]

    def enter(self, team: Team, variant: str = "VE", **standing: object) -> None:
        """Put a native team in the official table."""
        group = TeamGroup.objects.get_or_create(
            season=self.season,
            local_team=team,
            defaults={
                "club": self.source_club,
                "name": team.name,
                "normalized_name": str(team.id_uuid),
            },
        )[0]
        source = SourceTeam.objects.create(
            season=self.season,
            club=self.source_club,
            group=group,
            external_id=f"{self.pool.external_id}-{team.id_uuid}-{variant}",
            name=team.name,
            sport=variant,
        )
        PoolEntry.objects.create(pool=self.pool, team=source, standing=standing)

    def play(
        self, home: Team, away: Team, days: int, score: tuple[int, int] | None = None
    ) -> Match:
        """Schedule a poule fixture, finished when it has a score."""
        match = Match.objects.create(
            home_team=home,
            away_team=away,
            season=self.native_season,
            pool=self.local,
            start_time=timezone.now() + timedelta(days=days),
        )
        if score:
            MatchData.objects.filter(match_link=match).update(
                status="finished", home_score=score[0], away_score=score[1]
            )
        return match


@pytest.fixture
def season() -> Season:
    """Return a season that runs today."""
    today = timezone.localdate()
    return Season.objects.create(
        name="2026",
        start_date=today - timedelta(days=60),
        end_date=today + timedelta(days=200),
    )


def test_team_standings_report_position_form_and_next_opponent(
    client: Client, season: Season
) -> None:
    """A followed team shows its place, latest results and coming opponent."""
    rival_position = 3
    extra_teams = 3
    club = Club.objects.create(name="Own")
    team = Team.objects.create(name="1", club=club)
    rival = Team.objects.create(name="2", club=Club.objects.create(name="Rival"))
    leader = Team.objects.create(name="1", club=Club.objects.create(name="Leader"))
    poule = Poule(season, "1A")
    poule.enter(
        team,
        Position="2",
        TotalMatches=3,
        Won=1,
        Draw=1,
        Lost=1,
        TotalPoints=3,
        GoalsFor=50,
        GoalsAgainst=48,
    )
    poule.enter(leader, Position="1")
    poule.enter(rival, Position=str(rival_position))
    poule.play(team, rival, -21, (20, 15))
    poule.play(leader, team, -14, (18, 12))
    poule.play(team, leader, -7, (16, 16))
    upcoming = poule.play(rival, team, 7)
    poule.play(team, leader, 14)

    user = get_user_model().objects.create_user(username="standings")
    user.player.team_follow.add(team)
    client.force_login(user)

    with CaptureQueriesContext(connection) as first:
        response = client.get(URL)
    assert response.status_code == HTTPStatus.OK
    [row] = response.json()
    assert row["team"]["id_uuid"] == str(team.id_uuid)
    assert row["team"]["club"] == "Own"
    assert row["role"] == "following"
    assert row["pool"] == {
        "id": poule.pool.pk,
        "name": "1A",
        "class_name": "Senioren 1e klasse",
    }
    assert row["computed"] is False
    assert (row["position"], row["teams"]) == (2, 3)
    assert (row["played"], row["points"]) == (3, 3)
    assert (row["won"], row["drawn"], row["lost"]) == (1, 1, 1)
    assert row["form"] == ["W", "L", "D"]
    assert row["next_opponent"]["match_id"] == str(upcoming.id_uuid)
    assert row["next_opponent"]["club"] == "Rival"
    assert row["next_opponent"]["position"] == rival_position

    # More followed teams must not add queries per team.
    for index in range(extra_teams):
        extra = Team.objects.create(name=f"Extra {index}", club=club)
        poule.enter(extra, Position=str(4 + index))
        poule.play(extra, leader, -3, (10, 12))
        user.player.team_follow.add(extra)
    with CaptureQueriesContext(connection) as later:
        rows = client.get(URL).json()
    assert len(rows) == 1 + extra_teams
    assert len(later) == len(first)


def test_team_standings_follow_the_poule_being_played(
    client: Client, season: Season
) -> None:
    """A finished earlier phase gives way once the next poule has a result."""
    team = Team.objects.create(name="1", club=Club.objects.create(name="Own"))
    other = Team.objects.create(name="1", club=Club.objects.create(name="Other"))
    autumn = Poule(season, "Najaar")
    indoor = Poule(season, "Zaal")
    autumn.enter(team, Position="1")
    indoor.enter(team, Position="4")
    autumn.play(team, other, -40, (20, 10))
    indoor.play(team, other, 5)

    user = get_user_model().objects.create_user(username="phases")
    user.player.team_follow.add(team)
    client.force_login(user)

    assert client.get(URL).json()[0]["pool"]["name"] == "Najaar"

    indoor.play(other, team, -1, (14, 15))
    [row] = client.get(URL).json()
    assert row["pool"]["name"] == "Zaal"
    assert row["form"] == ["W"]


def test_team_standings_skip_unranked_poules_and_require_a_player(
    client: Client, season: Season
) -> None:
    """Club-filtered poules have no table, and anonymous callers have no teams."""
    assert client.get(URL).status_code in {
        HTTPStatus.UNAUTHORIZED,
        HTTPStatus.FORBIDDEN,
    }

    team = Team.objects.create(name="1", club=Club.objects.create(name="Own"))
    poule = Poule(season, "Jeugd")
    poule.enter(team, Position="1")
    Pool.objects.filter(pk=poule.pool.pk).update(results_filtered=True)
    unranked = Poule(season, "Zonder stand")
    unranked.enter(team)

    user = get_user_model().objects.create_user(username="unranked")
    user.player.team_follow.add(team)
    client.force_login(user)

    assert client.get(URL).json() == []


def test_team_standings_leave_out_a_poule_whose_native_season_ended(
    client: Client, season: Season
) -> None:
    """The import scope still runs; the season the poule was played in does not."""
    today = timezone.localdate()
    ended = Season.objects.create(
        name="2026 najaar",
        start_date=today - timedelta(days=120),
        end_date=today - timedelta(days=10),
    )
    team = Team.objects.create(name="1", club=Club.objects.create(name="Own"))
    other = Team.objects.create(name="1", club=Club.objects.create(name="Other"))
    autumn = Poule(season, "Ended poule", native_season=ended)
    running = Poule(season, "Running poule")
    autumn.enter(team, Position="1")
    running.enter(team, Position="3")
    autumn.play(team, other, -30, (20, 10))
    running.play(team, other, 5)

    user = get_user_model().objects.create_user(username="ended")
    user.player.team_follow.add(team)
    client.force_login(user)

    [row] = client.get(URL).json()
    assert row["pool"]["name"] == "Running poule"
    assert row["form"] == []


def test_team_standings_skip_called_off_fixtures_and_count_club_teams_once(
    client: Client, season: Season
) -> None:
    """A cancelled fixture is not the next match; source variants are one team."""
    second_position = 3
    team = Team.objects.create(name="1", club=Club.objects.create(name="Own"))
    first = Team.objects.create(name="1", club=Club.objects.create(name="First"))
    second = Team.objects.create(name="1", club=Club.objects.create(name="Second"))
    poule = Poule(season, "1A")
    poule.enter(team, Position="1")
    # The same club team is listed for the indoor and the outdoor competition.
    poule.enter(team, variant="ZA", Position="1")
    poule.enter(first, Position="2")
    # The unranked variant of the next opponent comes first in the table rows.
    poule.enter(second, variant="AA")
    poule.enter(second, variant="ZA", Position="3")
    cancelled = poule.play(team, first, 1)
    played = poule.play(team, second, 7)
    source_teams = {
        entry.team.group.local_team_id: entry.team
        for entry in PoolEntry.objects.select_related("team__group")
    }
    SourceMatch.objects.create(
        season=season,
        external_id="cancelled",
        pool=poule.pool,
        home_team=source_teams[team.id_uuid],
        away_team=source_teams[first.id_uuid],
        starts_at=cancelled.start_time,
        status="CANCELLED",
        local_match=cancelled,
    )

    user = get_user_model().objects.create_user(username="cancelled")
    user.player.team_follow.add(team)
    client.force_login(user)

    [row] = client.get(URL).json()
    assert row["next_opponent"]["match_id"] == str(played.id_uuid)
    assert row["next_opponent"]["club"] == "Second"
    assert row["next_opponent"]["position"] == second_position
    assert row["teams"] == Team.objects.count()


def test_team_standings_keep_places_for_teams_that_have_a_standing(
    client: Client, season: Season
) -> None:
    """Followed teams without a table do not push a ranked team off Home."""
    club = Club.objects.create(name="Own")
    user = get_user_model().objects.create_user(username="crowded")
    for index in range(MAX_HOME_TEAMS):
        user.player.team_follow.add(Team.objects.create(name=f"A{index}", club=club))
    ranked = [Team.objects.create(name=f"Z{index}", club=club) for index in range(8)]
    poule = Poule(season, "1A")
    for index, team in enumerate(ranked):
        poule.enter(team, Position=str(index + 1))
        user.player.team_follow.add(team)
    client.force_login(user)

    rows = client.get(URL).json()
    assert [row["team"]["name"] for row in rows] == [
        team.name for team in ranked[:MAX_HOME_TEAMS]
    ]
