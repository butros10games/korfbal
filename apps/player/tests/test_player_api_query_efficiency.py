"""Query-count regressions for player API serialization."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager

from django.contrib.auth import get_user_model
import pytest

from apps.club.models import Club
from apps.player.api.serializers import PlayerSerializer
from apps.player.api.views.common import player_detail_queryset
from apps.player.models import PlayerClubMembership


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
        PlayerClubMembership.objects.create(player=user.player, club=club)

    loaded_players = list(
        player_detail_queryset().filter(id_uuid__in=[player.pk for player in players])
    )

    with django_assert_num_queries(0):
        payload = PlayerSerializer(loaded_players, many=True).data

    assert len(payload) == len(players)
    assert all(len(row["active_member_clubs"]) == 1 for row in payload)
