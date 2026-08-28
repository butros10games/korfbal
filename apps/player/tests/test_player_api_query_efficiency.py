"""Query-count regressions for player API serialization."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from http import HTTPStatus

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import Client, override_settings
from django.test.utils import CaptureQueriesContext
import pytest

from apps.club.models import Club
from apps.player.api.serializers import PlayerSerializer
from apps.player.models import Player, PlayerClubMembership
from apps.player.services.player_queries import player_detail_queryset


@pytest.mark.django_db
def test_player_detail_serialization_uses_only_prefetched_data(
    django_assert_num_queries: Callable[[int], AbstractContextManager[None]],
) -> None:
    """Adding players must not add membership or song queries per result."""
    club = Club.objects.create(name="Serialization Club")
    players = []
    for index in range(4):
        user = get_user_model().objects.create_user(username=f"serializer-{index}")
        players.append(user.player)
        user.player.profile_picture_visibility = Player.Visibility.CLUB
        user.player.stats_visibility = Player.Visibility.CLUB
        user.player.teams_visibility = Player.Visibility.CLUB
        user.player.save(
            update_fields=[
                "profile_picture_visibility",
                "stats_visibility",
                "teams_visibility",
            ]
        )
        PlayerClubMembership.objects.create(player=user.player, club=club)

    loaded_players = list(
        player_detail_queryset().filter(id_uuid__in=[player.pk for player in players])
    )
    viewer = loaded_players[0]

    with django_assert_num_queries(0):
        payload = PlayerSerializer(
            loaded_players,
            many=True,
            context={"viewer_player": viewer},
        ).data

    assert len(payload) == len(players)
    assert all(len(row["active_member_clubs"]) == 1 for row in payload)
    assert all(row["can_view_stats"] is True for row in payload)


@pytest.mark.django_db
def test_player_list_queries_do_not_scale_with_club_restricted_players(
    client: Client,
) -> None:
    """Profile list privacy checks must not introduce per-player queries."""
    club = Club.objects.create(name="List query club")
    viewer_user = get_user_model().objects.create_user(username="list-query-viewer")
    PlayerClubMembership.objects.create(player=viewer_user.player, club=club)
    client.force_login(viewer_user)

    def create_target(index: int) -> None:
        user = get_user_model().objects.create_user(username=f"list-query-{index}")
        Player.objects.filter(pk=user.player.pk).update(
            profile_picture_visibility=Player.Visibility.CLUB,
            stats_visibility=Player.Visibility.CLUB,
            teams_visibility=Player.Visibility.CLUB,
        )
        PlayerClubMembership.objects.create(player=user.player, club=club)

    create_target(0)
    with CaptureQueriesContext(connection) as initial_queries:
        initial_response = client.get("/api/player/players/")
    for index in range(1, 5):
        create_target(index)
    with CaptureQueriesContext(connection) as expanded_queries:
        expanded_response = client.get("/api/player/players/")

    assert initial_response.status_code == HTTPStatus.OK
    assert expanded_response.status_code == HTTPStatus.OK
    assert len(expanded_queries) == len(initial_queries)


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_player_read_endpoints_keep_bounded_query_counts(client: Client) -> None:
    """Core profile read endpoints retain explicit fixed query budgets."""
    user = get_user_model().objects.create_user(username="read-query-budgets")
    client.force_login(user)
    routes = {
        "profile": f"/api/player/players/{user.player.id_uuid}/",
        "teams": "/api/player/me/teams/",
        "overview": "/api/player/me/overview/",
        "stats": "/api/player/me/stats/",
    }
    counts: dict[str, int] = {}

    for name, route in routes.items():
        with CaptureQueriesContext(connection) as queries:
            response = client.get(route)
        assert response.status_code == HTTPStatus.OK
        counts[name] = len(queries)

    assert counts == {
        "profile": 9,
        "teams": 11,
        "overview": 12,
        "stats": 15,
    }
