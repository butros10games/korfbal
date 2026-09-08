"""Catalogue API authentication and filter regression tests."""

from __future__ import annotations

from datetime import date

from django.contrib.auth import get_user_model
from django.db import connection
from django.test.utils import CaptureQueriesContext
import pytest
from rest_framework import status
from rest_framework.test import APIClient

from apps.club.models import Club as NativeClub
from apps.competition.models import Club, Pool, PoolEntry, Team, TeamGroup
from apps.schedule.models import Season
from apps.team.models import Team as NativeTeam


@pytest.mark.django_db
def test_catalogue_requires_json_auth() -> None:
    """Unauthenticated clients receive an API error, never a login redirect."""
    response = APIClient().get("/api/competition/clubs/")
    assert response.status_code in {401, 403}
    assert "application/json" in response["Content-Type"]


@pytest.mark.django_db
def test_catalogue_is_paginated_searchable_and_read_only() -> None:
    """Serve bounded local reads and reject writes even for logged-in users."""
    user = get_user_model().objects.create_user(username="catalogue")
    client = APIClient()
    client.force_authenticate(user)
    Club.objects.create(external_id="C1", name="Example Club")
    response = client.get("/api/competition/clubs/?search=Example&page_size=1")
    assert response.status_code == status.HTTP_200_OK
    assert response.data["count"] == 1
    assert response.data["results"][0]["name"] == "Example Club"
    assert (
        client.post("/api/competition/clubs/", {}).status_code
        == status.HTTP_405_METHOD_NOT_ALLOWED
    )
    assert (
        client.get("/api/competition/teams/?season=invalid").status_code
        == status.HTTP_400_BAD_REQUEST
    )


@pytest.mark.django_db
def test_season_api_exposes_filter_identifiers() -> None:
    """Clients can discover season IDs without out-of-band database access."""
    season = Season.objects.create(
        name="2026", start_date=date(2026, 7, 1), end_date=date(2027, 6, 30)
    )
    client = APIClient()
    client.force_authenticate(get_user_model().objects.create_user(username="seasons"))
    response = client.get("/api/competition/seasons/")
    assert response.status_code == status.HTTP_200_OK
    assert response.data["results"][0]["id_uuid"] == str(season.pk)


@pytest.mark.django_db
def test_standings_preserve_unknown_values_and_hide_filtered_results(
    season: Season,
) -> None:
    """Expose stable numbers without turning missing data into a zero score."""
    from_payload = {"Position": "1", "TotalMatches": 0, "TotalPoints": -1, "Won": "NaN"}
    club = Club.objects.create(external_id="synthetic", name="Synthetic")
    team = Team.objects.create(
        season=season,
        club=club,
        external_id="T",
        name="Synthetic 1",
        sport="KORFBALL-VE-WK",
    )
    pool = Pool.objects.create(
        season=season,
        external_id="P",
        name="Poule A",
        sport="KORFBALL-VE-WK",
        results_filtered=False,
    )
    PoolEntry.objects.create(pool=pool, team=team, standing=from_payload)
    client = APIClient()
    client.force_authenticate(
        get_user_model().objects.create_user(username="standings")
    )
    response = client.get(
        f"/api/competition/pools/{pool.pk}/standings/?search=unrelated"
    )
    assert response.status_code == status.HTTP_200_OK
    values = response.data["results"][0]["values"]
    assert values == {
        "position": 1,
        "played": 0,
        "points": -1,
        "won": None,
        "drawn": None,
        "lost": None,
        "goals_for": None,
        "goals_against": None,
    }
    pool.results_filtered = True
    pool.save(update_fields=["results_filtered"])
    assert (
        client.get(f"/api/competition/pools/{pool.pk}/standings/").data["results"] == []
    )


@pytest.mark.django_db
def test_official_positions_sort_numerically_before_pagination(season: Season) -> None:
    """Provider string positions sort as ranks and missing ranks stay last."""
    club = Club.objects.create(external_id="rank-club", name="Example")
    pool = Pool.objects.create(
        season=season, external_id="rank-pool", results_filtered=False
    )
    for index, position in enumerate(["10", None, "2"]):
        team = Team.objects.create(
            season=season,
            club=club,
            external_id=f"rank-{index}",
            name=f"Team {index}",
            sport="VE",
        )
        PoolEntry.objects.create(pool=pool, team=team, standing={"Position": position})
    client = APIClient()
    client.force_authenticate(get_user_model().objects.create_user(username="ranks"))
    expected_first_rank = 2
    response = client.get(f"/api/competition/pools/{pool.pk}/standings/?page_size=1")
    assert response.data["results"][0]["values"]["position"] == expected_first_rank
    assert response.data["next"] is not None


@pytest.mark.django_db
def test_team_standings_are_bounded_and_constant_query_count(season: Season) -> None:
    """Batch pools without N+1 reads or fetching every team's full standings."""
    expected_batch_queries = 5
    expected_page_queries = 4
    standings_page_size = 100
    native_club = NativeClub.objects.create(name="Synthetic")
    native_team = NativeTeam.objects.create(name="1", club=native_club)
    club = Club.objects.create(external_id="bounded", name="Synthetic")
    group = TeamGroup.objects.create(
        season=season, club=club, name="1", local_team=native_team
    )
    teams = Team.objects.bulk_create([
        Team(
            season=season,
            club=club,
            group=group if index == 0 else None,
            external_id=f"bounded-{index}",
            name=f"Synthetic {index}",
            sport="VE",
        )
        for index in range(102)
    ])
    pools = Pool.objects.bulk_create([
        Pool(
            season=season,
            external_id=f"bounded-{index}",
            name=f"Pool {index}",
            results_filtered=index == len(["first", "second"]),
        )
        for index in range(3)
    ])
    PoolEntry.objects.bulk_create([
        PoolEntry(pool=pool, team=team, standing={"Position": str(102 - index)})
        for pool in pools
        for index, team in enumerate(teams)
    ])
    client = APIClient()
    client.force_authenticate(get_user_model().objects.create_user(username="bounded"))
    url = (
        f"/api/competition/pools/team-standings/?local_team={native_team.pk}"
        f"&season={season.pk}"
    )
    for page_size in [1, 12]:
        with CaptureQueriesContext(connection) as queries:
            response = client.get(f"{url}&page_size={page_size}")
        assert response.status_code == status.HTTP_200_OK
        assert len(queries) == expected_batch_queries
        assert response.data["count"] == len(pools)
        rows = response.data["results"]
        assert len(rows) == min(page_size, 3)
        for pool in rows:
            assert "teams" not in pool
            standings = pool["standings"]
            if pool["results_filtered"]:
                assert standings == {"results": [], "has_more": False}
            else:
                assert len(standings["results"]) == standings_page_size
                assert standings["has_more"] is True
                assert [
                    row["values"]["position"] for row in standings["results"]
                ] == list(range(1, 101))
                assert "standing" not in standings["results"][0]

    with CaptureQueriesContext(connection) as queries:
        later = client.get(
            f"/api/competition/pools/{pools[0].pk}/standings/?page=2&page_size=100"
        )
    assert len(queries) == expected_page_queries
    assert [row["values"]["position"] for row in later.data["results"]] == [101, 102]
    assert later.data["next"] is None
    assert (
        client.get("/api/competition/pools/team-standings/").status_code
        == status.HTTP_400_BAD_REQUEST
    )
