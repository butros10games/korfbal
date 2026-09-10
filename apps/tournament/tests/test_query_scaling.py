"""Keep tournament reads bounded as the number of events and pools grows."""

from unittest.mock import Mock

from django.contrib.auth import get_user_model
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
import pytest
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.tournament.api.views import TournamentViewSet
from apps.tournament.models import (
    Tournament,
    TournamentField,
    TournamentMatch,
    TournamentMember,
    TournamentPool,
    TournamentPoolEntry,
    TournamentStage,
    TournamentTeam,
)
from apps.tournament.services.snapshot import build_tournament_snapshot


pytestmark = pytest.mark.django_db
SNAPSHOT_QUERIES = 8
LIST_QUERIES = 1


@pytest.mark.parametrize("pool_count", [1, 6])
@pytest.mark.parametrize("cup_enabled", [False, True])
def test_snapshot_queries_do_not_grow_per_pool(
    pool_count: int,
    cup_enabled: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both live and final standings must consume the snapshot's prefetched data."""
    owner = get_user_model().objects.create_user(username="snapshot-owner")
    tournament = Tournament.objects.create(
        owner=owner,
        name="Query cup",
        slug="query-cup",
        starts_at=timezone.now(),
        cup_rules={
            "regular_minutes": [30, 30],
            "extra_minutes": [5, 5],
            "shootout_attempts": 3,
        }
        if cup_enabled
        else {},
    )
    stage = TournamentStage.objects.create(
        tournament=tournament, name="Pools", kind=TournamentStage.Kind.POOL
    )
    for index in range(pool_count):
        pool = TournamentPool.objects.create(
            tournament=tournament, stage=stage, name=f"Pool {index}"
        )
        home, away = [
            TournamentTeam.objects.create(tournament=tournament, name=f"{index} {side}")
            for side in ("Home", "Away")
        ]
        for team in (home, away):
            TournamentPoolEntry.objects.create(pool=pool, team=team)
        TournamentMatch.objects.create(
            tournament=tournament,
            stage=stage,
            pool=pool,
            home_team=home,
            away_team=away,
            status=TournamentMatch.Status.LIVE,
            home_score=1,
            away_score=0,
            match_number=index + 1,
        )
    tournament = Tournament.objects.select_related("display_config").get(
        pk=tournament.pk
    )
    hydrate_tournament = Mock(wraps=Tournament.from_db.__func__)
    monkeypatch.setattr(Tournament, "from_db", classmethod(hydrate_tournament))
    with CaptureQueriesContext(connection) as queries:
        snapshot = build_tournament_snapshot(tournament)
    # Reuse the aggregate loaded by the request instead of rebuilding it per match.
    hydrate_tournament.assert_not_called()
    for query in queries:
        if f'FROM "{TournamentMatch._meta.db_table}"' not in query["sql"]:
            continue
        assert "referee_claim_token" not in query["sql"]
        assert "referee_access_token" not in query["sql"]
        with connection.cursor() as cursor:
            cursor.execute(query["sql"])
            maximum_selected_columns = 50
            assert len(cursor.description) <= maximum_selected_columns
    assert all(bool(match["cup"]) == cup_enabled for match in snapshot["matches"])
    assert len(queries) == SNAPSHOT_QUERIES
    assert len(snapshot["pools"]) == pool_count
    assert all(pool["standings"][0]["played"] == 1 for pool in snapshot["pools"])


@pytest.mark.parametrize("tournament_count", [1, 6])
def test_tournament_list_counts_and_roles_have_constant_queries(
    tournament_count: int,
) -> None:
    """Manager flags and independent child counts are read with the page query."""
    owner = get_user_model().objects.create_user(username="list-owner")
    viewer = get_user_model().objects.create_user(username="list-viewer")
    expected = {}
    for index in range(tournament_count):
        tournament = Tournament.objects.create(
            owner=owner,
            name=f"Cup {index}",
            slug=f"cup-{index}",
            starts_at=timezone.now(),
            status=Tournament.Status.PUBLISHED,
        )
        TournamentMember.objects.create(
            tournament=tournament,
            user=viewer,
            role=(
                TournamentMember.Role.MANAGER
                if index % 2 == 0
                else TournamentMember.Role.SCOREKEEPER
            ),
        )
        for side in ("Home", "Away"):
            TournamentTeam.objects.create(tournament=tournament, name=side)
        TournamentField.objects.create(tournament=tournament, label="Main")
        expected[str(tournament.pk)] = index % 2 == 0
    request = APIRequestFactory().get("/api/tournaments/")
    force_authenticate(request, user=viewer)
    with CaptureQueriesContext(connection) as queries:
        response = TournamentViewSet.as_view({"get": "list"})(request)
        response.render()
    assert len(queries) == LIST_QUERIES
    assert len(response.data) == tournament_count
    for row in response.data:
        assert row["can_manage"] is expected[row["id_uuid"]]
        assert row["team_count"] == len(("Home", "Away"))
        assert row["field_count"] == 1
        assert row["match_count"] == 0
