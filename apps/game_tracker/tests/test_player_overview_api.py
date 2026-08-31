"""Focused contracts for the player-overview endpoint."""

from http import HTTPStatus

from django.db import connection
from django.test.client import Client
from django.test.utils import CaptureQueriesContext
import pytest

from apps.game_tracker.models import PlayerGroup
from apps.game_tracker.tests.tracker_test_helpers import (
    create_group_types,
    create_tracker_match,
    create_tracker_player,
    get_tracker_group,
)


pytestmark = pytest.mark.django_db

GROUPS_AFTER_ONE_TYPE = 2
GROUPS_AFTER_THREE_TYPES = 6
GROUPS_PER_TEAM = 3


def _overview_url(match_id: object, team_id: object) -> str:
    return f"/api/match/player_overview_data/{match_id}/{team_id}/"


def test_group_types_backfill_both_match_teams() -> None:
    """Creating group types backfills one group per team and type."""
    tracker = create_tracker_match(prefix="Overview Backfill")

    assert PlayerGroup.objects.count() == 0

    create_group_types("Aanval")
    assert PlayerGroup.objects.count() == GROUPS_AFTER_ONE_TYPE

    create_group_types("Verdediging", "Reserve")
    assert PlayerGroup.objects.count() == GROUPS_AFTER_THREE_TYPES
    assert (
        PlayerGroup.objects.filter(
            match_data=tracker.match_data,
            team=tracker.home_team,
        ).count()
        == GROUPS_PER_TEAM
    )
    assert (
        PlayerGroup.objects.filter(
            match_data=tracker.match_data,
            team=tracker.away_team,
        ).count()
        == GROUPS_PER_TEAM
    )


def test_player_overview_returns_groups_and_live_revision(client: Client) -> None:
    """The overview exposes its groups and the aggregate revision."""
    tracker = create_tracker_match(prefix="Overview Response")
    tracker.match_data.status = "active"
    tracker.match_data.save(update_fields=["status"])
    create_group_types("Aanval", "Verdediging", "Reserve")

    response = client.get(
        _overview_url(tracker.match.id_uuid, tracker.home_team.id_uuid)
    )

    assert response.status_code == HTTPStatus.OK
    payload = response.json()
    assert len(payload["player_groups"]) == GROUPS_PER_TEAM
    assert payload["live_revision"] == tracker.match_data.live_revision
    assert PlayerGroup.objects.count() == GROUPS_AFTER_THREE_TYPES


def test_player_overview_query_count_does_not_scale_with_players(
    client: Client,
) -> None:
    """Adding players does not add queries to the overview read."""
    tracker = create_tracker_match(prefix="Overview Queries")
    create_group_types("Aanval", "Verdediging", "Reserve")
    groups = [
        get_tracker_group(tracker, name)
        for name in ("Aanval", "Verdediging", "Reserve")
    ]
    for group, name in zip(groups, ("attack", "defense", "reserve"), strict=True):
        group.players.add(create_tracker_player(username=f"overview_{name}_1"))
    url = _overview_url(tracker.match.id_uuid, tracker.home_team.id_uuid)

    with CaptureQueriesContext(connection) as warm_queries:
        warm_response = client.get(url)

    for suffix in range(2, 5):
        for group, name in zip(
            groups,
            ("attack", "defense", "reserve"),
            strict=True,
        ):
            group.players.add(
                create_tracker_player(username=f"overview_{name}_{suffix}")
            )

    with CaptureQueriesContext(connection) as expanded_queries:
        expanded_response = client.get(url)

    assert warm_response.status_code == HTTPStatus.OK
    assert expanded_response.status_code == HTTPStatus.OK
    assert len(expanded_queries) == len(warm_queries)
