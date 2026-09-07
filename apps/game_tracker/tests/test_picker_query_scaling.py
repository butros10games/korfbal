"""Picker reads keep privacy checks bounded as club rosters grow."""

from http import HTTPStatus

from django.db import connection
from django.test.client import Client
from django.test.utils import CaptureQueriesContext
import pytest

from apps.game_tracker.tests.tracker_test_helpers import (
    connect_user_to_match_club,
    create_group_types,
    create_player_group,
    create_tracker_match,
    create_tracker_player,
    login_home_club_editor,
)
from apps.player.models import Player
from apps.team.models import TeamData


EXPANDED_ROSTER_SIZE = 9


@pytest.mark.django_db
@pytest.mark.parametrize(
    "endpoint", ["player_search", "players_team", "player_overview_data"]
)
def test_picker_privacy_queries_do_not_grow_with_roster(
    client: Client, endpoint: str
) -> None:
    """Club-only pictures retain visibility without per-result membership reads."""
    tracker = create_tracker_match(prefix=f"Picker {endpoint}")
    login_home_club_editor(client, tracker, f"editor_{endpoint}")
    roster = TeamData.objects.create(
        team=tracker.home_team, season=tracker.match.season
    )
    group_type = create_group_types("Query Attack")["Query Attack"]
    group = create_player_group(
        match_data=tracker.match_data, team=tracker.home_team, group_type=group_type
    )

    def add_candidate(index: int) -> None:
        player = create_tracker_player(username=f"candidate_{endpoint}_{index}")
        player.profile_picture_visibility = Player.Visibility.CLUB
        player.profile_picture = f"players/synthetic-{index}.png"
        player.save(update_fields=["profile_picture_visibility", "profile_picture"])
        connect_user_to_match_club(player.user, tracker.home_team.club, tracker.match)
        roster.players.add(player)
        if endpoint == "player_overview_data":
            group.players.add(player)

    url = (
        f"/api/match/{endpoint}/{tracker.match.id_uuid}/{tracker.home_team.id_uuid}/"
        "?search=candidate"
    )
    add_candidate(0)
    with CaptureQueriesContext(connection) as initial:
        response = client.get(url)
    assert response.status_code == HTTPStatus.OK
    for index in range(1, EXPANDED_ROSTER_SIZE):
        add_candidate(index)
    with CaptureQueriesContext(connection) as expanded:
        response = client.get(url)
    assert response.status_code == HTTPStatus.OK
    payload = response.json()
    players = (
        [player for group in payload["player_groups"] for player in group["players"]]
        if endpoint == "player_overview_data"
        else payload["players"]
    )
    assert len(players) == EXPANDED_ROSTER_SIZE
    assert all("synthetic-" in player["get_profile_picture"] for player in players)
    assert len(expanded) <= len(initial), (len(initial), len(expanded))
