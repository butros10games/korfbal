"""Team and club history compare every season without scanning the catalogue."""

from __future__ import annotations

from datetime import UTC, date, datetime
from http import HTTPStatus

from django.db import connection
from django.test.client import Client
from django.test.utils import CaptureQueriesContext
import pytest

from apps.club.models import Club
from apps.competition.models import (
    Club as SourceClub,
    CompetitionClass,
    CompetitionEdition,
    Match as SourceMatch,
    MatchRating,
    Pool,
    PoolEntry,
    Team as SourceTeam,
    TeamGroup,
)
from apps.game_tracker.models import MatchData
from apps.schedule.models import Match, Season, SeasonPool
from apps.team.models import Team


pytestmark = pytest.mark.django_db
# Results (home and away), poules, ratings (home and away), seasons, teams, club.
MAX_HISTORY_QUERIES = 9
LADDER_FIELDS = {"ladder_id", "hierarchy_revision", "level_reason"}


def season(name: str, edition: int, phase: str, start: date, end: date) -> Season:
    """Create a season with stored competition context."""
    return Season.objects.create(
        name=name,
        start_date=start,
        end_date=end,
        edition=edition,
        discipline="indoor" if phase == "indoor" else "outdoor",
        phase=phase,
    )


def play(
    fixture: tuple[Team, Team],
    played_in: Season,
    score: tuple[int, int],
    *,
    pool: SeasonPool | None = None,
    status: str = "finished",
) -> Match:
    """Create a native fixture with a final score."""
    home, away = fixture
    match = Match.objects.create(
        home_team=home,
        away_team=away,
        season=played_in,
        pool=pool,
        start_time=datetime.combine(played_in.start_date, datetime.min.time(), UTC),
    )
    MatchData.objects.filter(match_link=match).update(
        status=status, home_score=score[0], away_score=score[1]
    )
    return match


def standing(
    team: Team,
    local_pool: SeasonPool,
    *,
    level: int,
    positions: tuple[str, ...],
    results_filtered: bool = False,
) -> None:
    """Link a native poule to a classified source poule with standings.

    The first position is the team's; the others belong to its opponents.
    """
    played_in = local_pool.season
    edition = CompetitionEdition.objects.create(
        season=played_in, discipline="indoor", phase="indoor", gender="mixed"
    )
    classification = CompetitionClass.objects.create(
        edition=edition,
        code="hoofdklasse",
        category="a",
        age_group="senior",
        team_kind="first",
        colour="unknown",
        playing_format="eight",
        level=level,
    )
    pool = Pool.objects.create(
        season=played_in,
        external_id=f"pool-{local_pool.pk}",
        name=local_pool.name,
        class_name="Hoofdklasse",
        competition_class=classification,
        local_pool=local_pool,
        results_filtered=results_filtered,
    )
    source_club, _ = SourceClub.objects.get_or_create(
        external_id=f"club-{team.club_id}", defaults={"name": team.club.name}
    )
    group = TeamGroup.objects.create(
        season=played_in,
        club=source_club,
        name=team.name,
        normalized_name=team.name,
        local_team=team,
    )
    source_team = SourceTeam.objects.create(
        season=played_in,
        external_id=f"team-{team.pk}-{played_in.pk}",
        club=source_club,
        name=team.name,
        sport="KORFBALL-ZA",
        group=group,
    )
    PoolEntry.objects.create(
        pool=pool, team=source_team, standing={"Position": positions[0]}
    )
    for index, position in enumerate(positions[1:]):
        other = SourceTeam.objects.create(
            season=played_in,
            external_id=f"other-{local_pool.pk}-{index}",
            club=source_club,
            name=f"Other {index}",
            sport="KORFBALL-ZA",
        )
        PoolEntry.objects.create(pool=pool, team=other, standing={"Position": position})


def rate(
    team: Team,
    played_in: Season,
    day: int,
    rating: tuple[float, float],
    *,
    home: bool = True,
) -> None:
    """Store the team's Elo before a source match and its change in that match."""
    before, change = rating
    source_club, _ = SourceClub.objects.get_or_create(
        external_id="rated", defaults={"name": "Rated"}
    )
    teams = [
        SourceTeam.objects.create(
            season=played_in,
            external_id=f"rated-{played_in.pk}-{day}-{side}",
            club=source_club,
            name=side,
            sport="KORFBALL-ZA",
        )
        for side in ("home", "away")
    ]
    starts_at = datetime(played_in.start_date.year, 1, day, tzinfo=UTC)
    source = SourceMatch.objects.create(
        season=played_in,
        external_id=f"rated-{played_in.pk}-{day}",
        home_team=teams[0],
        away_team=teams[1],
        starts_at=starts_at,
        status="played",
    )
    MatchRating.objects.create(
        match=source,
        home_rating=before if home else 1500,
        away_rating=1500 if home else before,
        home_expected=0.5,
        home_change=change if home else -change,
        home_games=10,
        away_games=10,
        home_team=team if home else None,
        away_team=None if home else team,
        starts_at=starts_at,
        phase=played_in,
    )


def test_team_history_lists_every_season_with_results_poule_and_elo(
    client: Client,
) -> None:
    """Combine final scores, official position, class level and Elo per season."""
    club = Club.objects.create(name="Historic")
    team = Team.objects.create(name="1", club=club)
    rival = Team.objects.create(name="1", club=Club.objects.create(name="Rival"))
    old = season("Zaal 2023", 2023, "indoor", date(2023, 11, 1), date(2024, 3, 1))
    new = season("Zaal 2024", 2024, "indoor", date(2024, 11, 1), date(2025, 3, 1))
    old_pool = SeasonPool.objects.create(season=old, name="H1")
    new_pool = SeasonPool.objects.create(season=new, name="O1")
    play((team, rival), old, (20, 15), pool=old_pool)
    play((rival, team), old, (18, 18), pool=old_pool)
    play((rival, team), old, (22, 17), pool=old_pool)
    play((team, rival), old, (0, 0), pool=old_pool, status="upcoming")
    play((team, rival), new, (25, 10), pool=new_pool)
    standing(
        team, old_pool, level=3, positions=("5", "1", "2", "3", "4", "6", "7", "8")
    )
    standing(team, new_pool, level=2, positions=("1",), results_filtered=True)
    rate(team, old, 2, (1600, 10))
    rate(team, old, 9, (1620, 5), home=False)
    rate(team, old, 5, (1610, 10))

    response = client.get(f"/api/team/teams/{team.pk}/history/")

    assert response.status_code == HTTPStatus.OK
    newest, oldest = response.json()["seasons"]
    assert (newest["season_name"], newest["phase"]) == ("Zaal 2024", "indoor")
    [poule] = newest["poules"]
    assert {"ladder_id", "hierarchy_revision", "level_reason"} <= poule.keys()
    assert {key: poule[key] for key in poule.keys() - LADDER_FIELDS} == {
        "id": str(new_pool.pk),
        "name": "O1",
        "class_name": "Hoofdklasse",
        "class_code": "hoofdklasse",
        "level": 2,
        "competition_part": None,
        # A table filtered to one club's results is no reliable position.
        "position": None,
        "observed_position": None,
        "teams": None,
        "table_source": "none",
        "table_status": "unknown",
        "fixture_coverage": "unknown",
        "penalty_points": None,
        "tied": False,
        "is_champion": False,
    }
    assert newest["rating"] is None
    assert {
        key: oldest[key]
        for key in (
            "edition",
            "kind",
            "played",
            "won",
            "drawn",
            "lost",
            "goals_for",
            "goals_against",
            "rating",
            "rating_change",
        )
    } == {
        "edition": 2023,
        "kind": "indoor",
        "played": 3,
        "won": 1,
        "drawn": 1,
        "lost": 1,
        "goals_for": 55,
        "goals_against": 55,
        # The away match on day 9 is the last one: 1620 + 5.
        "rating": 1625.0,
        "rating_change": 25.0,
    }
    old_poule = oldest["poules"][0]
    # An official table without final evidence is observed, not a final position.
    assert (
        old_poule["position"],
        old_poule["observed_position"],
        old_poule["teams"],
        old_poule["table_source"],
        old_poule["table_status"],
    ) == (None, 5, 8, "official", "unknown")


def test_history_never_turns_a_first_place_into_a_championship(
    client: Client,
) -> None:
    """Older apps call position 1 a champion; only final official evidence may."""
    team = Team.objects.create(name="1", club=Club.objects.create(name="Leaders"))
    generated = SeasonPool.objects.create(
        season=season("Zaal 2022", 2022, "indoor", date(2022, 11, 1), date(2023, 3, 1)),
        name="G1",
    )
    final = SeasonPool.objects.create(
        season=season("Zaal 2023", 2023, "indoor", date(2023, 11, 1), date(2024, 3, 1)),
        name="F1",
    )
    standing(team, generated, level=1, positions=("1", "2"))
    standing(team, final, level=1, positions=("1", "1", "3"))
    for entry in PoolEntry.objects.filter(pool__local_pool=generated):
        # A generated table with equal points orders its leaders approximately.
        entry.computed_standing = {**entry.standing, "TotalPoints": 4}
        entry.standing = {}
        entry.save(update_fields=("standing", "computed_standing"))
    PoolEntry.objects.filter(
        pool__local_pool=final, team__group__local_team=team
    ).update(standing={"Position": "1", "TotalPoints": 9, "PenaltyPoints": 2})
    Pool.objects.filter(local_pool=final).update(
        standings_provenance={
            "official_digest": "a" * 32,
            "official": {
                "status": "final",
                "evidence": {
                    "kind": "official-final-review-v1",
                    "reference": "synthetic reviewed source",
                    "table_digest": "a" * 32,
                },
            },
        }
    )

    newest, oldest = client.get(f"/api/team/teams/{team.pk}/history/").json()["seasons"]

    fields = (
        "position",
        "observed_position",
        "teams",
        "table_source",
        "table_status",
        "penalty_points",
        "tied",
        "is_champion",
    )
    assert {key: oldest["poules"][0][key] for key in fields} == {
        "position": None,
        "observed_position": 1,
        "teams": 2,
        "table_source": "computed",
        "table_status": "provisional",
        "penalty_points": None,
        "tied": True,
        "is_champion": False,
    }
    # A proven final official first place is a shared pool win, not a title.
    assert {key: newest["poules"][0][key] for key in fields} == {
        "position": 1,
        "observed_position": 1,
        "teams": 3,
        "table_source": "official",
        "table_status": "final",
        "penalty_points": 2,
        "tied": True,
        "is_champion": False,
    }


def test_club_history_totals_each_korfbal_year_and_lists_active_teams_first(
    client: Client,
) -> None:
    """Indoor and outdoor seasons of one July-June year add up together."""
    club = Club.objects.create(name="Historic")
    first = Team.objects.create(name="1", club=club)
    second = Team.objects.create(name="2", club=club)
    tenth = Team.objects.create(name="10", club=club)
    retired = Team.objects.create(name="A1", club=club)
    Team.objects.create(name="Without matches", club=club)
    other = Team.objects.create(name="1", club=Club.objects.create(name="Other"))
    autumn = season("Veld 2024", 2024, "autumn", date(2024, 9, 1), date(2024, 10, 31))
    indoor = season("Zaal 2024", 2024, "indoor", date(2024, 11, 1), date(2025, 3, 1))
    older = season("Zaal 2019", 2019, "indoor", date(2019, 11, 1), date(2020, 3, 1))
    play((first, other), autumn, (20, 10))
    play((other, first), indoor, (12, 12))
    play((second, other), indoor, (8, 11))
    play((tenth, other), indoor, (9, 7))
    play((other, retired), older, (14, 15))
    # Two club teams meeting each other count once for each team.
    play((first, second), indoor, (16, 14))

    with CaptureQueriesContext(connection) as queries:
        response = client.get(f"/api/club/clubs/{club.pk}/history/")

    assert response.status_code == HTTPStatus.OK
    data = response.json()
    assert [edition["key"] for edition in data["editions"]] == ["2024", "2019"]
    assert data["editions"][0] == {
        "key": "2024",
        "edition": 2024,
        "season_name": None,
        "start_date": "2024-09-01",
        "teams": 3,
        "played": 6,
        "won": 3,
        "drawn": 1,
        "lost": 2,
        "goals_for": 79,
        "goals_against": 70,
    }
    assert [team["name"] for team in data["teams"]] == ["1", "2", "10", "A1"]
    assert data["teams"][0] == {
        "id": str(first.pk),
        "name": "1",
        "seasons": 2,
        "first_season": "Veld 2024",
        "last_season": "Zaal 2024",
        "played": 3,
    }
    # Bounded by the number of history sources, not by teams or seasons.
    assert len(queries) <= MAX_HISTORY_QUERIES


def test_history_of_an_unknown_team_or_club_is_not_found(client: Client) -> None:
    """Unknown identifiers do not fall back to an empty history."""
    missing = "00000000-0000-7000-8000-000000000000"
    assert (
        client.get(f"/api/team/teams/{missing}/history/").status_code
        == HTTPStatus.NOT_FOUND
    )
    assert (
        client.get(f"/api/club/clubs/{missing}/history/").status_code
        == HTTPStatus.NOT_FOUND
    )
