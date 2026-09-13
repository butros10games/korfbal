"""Public pool browsing stays season-scoped, complete, and status-aware."""

from datetime import timedelta
from http import HTTPStatus

from django.test.client import Client
from django.utils import timezone
import pytest

from apps.club.models import Club
from apps.game_tracker.models import MatchData
from apps.schedule.models import Match, SeasonPool
from apps.team.models import Team

from .team_test_support import create_season


pytestmark = pytest.mark.django_db
type PoolContext = tuple[SeasonPool, tuple[Team, Team, Team]]


@pytest.fixture
def pool_context() -> PoolContext:
    """Three teams share one explicitly assigned season pool."""
    club = Club.objects.create(name="Pool club")
    teams = [Team.objects.create(name=f"Team {i}", club=club) for i in range(3)]
    pool = SeasonPool.objects.create(season=create_season(), name="A")
    pool.teams.set(teams)
    return pool, (teams[0], teams[1], teams[2])


def create_match(
    home: Team,
    away: Team,
    pool: SeasonPool,
    *,
    days: int = 1,
    status: str = "upcoming",
) -> Match:
    """Build a native fixture with a canonical tracker status and result."""
    match = Match.objects.create(
        home_team=home,
        away_team=away,
        season=pool.season,
        pool=pool,
        start_time=timezone.now() + timedelta(days=days),
    )
    MatchData.objects.filter(match_link=match).update(
        status=status, home_score=21, away_score=18
    )
    return match


def test_pool_matches_include_other_teams_and_exclude_other_pools_and_seasons(
    client: Client, pool_context: PoolContext
) -> None:
    """Keep public pool reads constrained to the requested context."""
    pool, (team, opponent, third) = pool_context
    own = create_match(team, opponent, pool, days=2)
    other = create_match(opponent, third, pool)
    live = create_match(opponent, team, pool, days=-1, status="active")
    create_match(team, third, pool, status="finished")
    unassigned = create_match(team, opponent, pool)
    Match.objects.filter(pk=unassigned.pk).update(pool=None)
    unrelated = SeasonPool.objects.create(season=pool.season, name="B")
    unrelated.teams.set([opponent, third])
    create_match(opponent, third, unrelated)
    past = SeasonPool.objects.create(
        season=create_season("Past", starts_in_days=-800, ends_in_days=-400), name="A"
    )
    past.teams.set([team, opponent])
    past_match = create_match(team, opponent, past)
    url = f"/api/team/teams/{team.pk}/pool-matches/"

    response = client.get(url, {"season": pool.season_id, "status": "upcoming"})
    assert response.status_code == HTTPStatus.OK
    data = response.json()
    assert data["count"] == len([live, other, own])
    assert [row["id_uuid"] for row in data["results"]] == [
        str(live.pk),
        str(other.pk),
        str(own.pk),
    ]
    response = client.get(url, {"season": past.season_id, "status": "upcoming"})
    assert [row["id_uuid"] for row in response.json()["results"]] == [
        str(past_match.pk)
    ]


def test_finished_pool_matches_page_through_all_results_without_duplicates(
    client: Client, pool_context: PoolContext
) -> None:
    """Keep public pool reads constrained to the requested context."""
    pool, (team, opponent, third) = pool_context
    second_pool = SeasonPool.objects.create(season=pool.season, name="Second pool")
    second_pool.teams.set([team, opponent, third])
    matches = [
        create_match(
            opponent,
            third,
            pool if i % 2 else second_pool,
            days=-i,
            status="finished",
        )
        for i in range(12)
    ]
    create_match(team, opponent, pool, status="upcoming")
    url = f"/api/team/teams/{team.pk}/pool-matches/"
    params = {"season": pool.season_id, "status": "finished", "page_size": 5}
    ids = []
    last_page = 3
    for page in range(1, last_page + 1):
        response = client.get(url, {**params, "page": page})
        assert response.status_code == HTTPStatus.OK
        data = response.json()
        assert data["count"] == len(matches)
        assert bool(data["next"]) is (page < last_page)
        assert bool(data["previous"]) is (page > 1)
        assert all(row["score"] == {"home": 21, "away": 18} for row in data["results"])
        ids.extend(row["id_uuid"] for row in data["results"])
    assert ids == [str(match.pk) for match in matches]


def test_no_pool_membership_returns_empty_instead_of_global_matches(
    client: Client, pool_context: PoolContext
) -> None:
    """Keep public pool reads constrained to the requested context."""
    pool, (team, opponent, third) = pool_context
    create_match(opponent, third, pool)
    pool.teams.remove(team)
    response = client.get(
        f"/api/team/teams/{team.pk}/pool-matches/",
        {"season": pool.season_id, "status": "upcoming"},
    )
    assert response.status_code == HTTPStatus.OK
    assert response.json()["results"] == []


@pytest.mark.parametrize(
    "invalid",
    [
        {"season": "invalid"},
        {"season": "11111111-1111-4111-8111-111111111111"},
        {"season": ""},
        {"status": "all"},
        {"status": ""},
    ],
)
def test_invalid_pool_filters_return_json_400(
    client: Client, pool_context: PoolContext, invalid: dict[str, str]
) -> None:
    """Keep public pool reads constrained to the requested context."""
    pool, (team, _, _) = pool_context
    response = client.get(
        f"/api/team/teams/{team.pk}/pool-matches/",
        {"season": pool.season_id, "status": "upcoming", **invalid},
    )
    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert response.headers["Content-Type"] == "application/json"
