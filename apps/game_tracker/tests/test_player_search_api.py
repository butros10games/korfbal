"""API tests for match-scoped player search."""

from http import HTTPStatus

from django.test.client import Client
import pytest

from apps.game_tracker.models import PlayerGroup
from apps.game_tracker.tests.tracker_test_helpers import (
    connect_user_to_match_club,
    create_group_types,
    create_tracker_match,
    create_tracker_player,
    create_tracker_user,
    login_home_club_editor,
)
from apps.team.models import Team, TeamData


pytestmark = pytest.mark.django_db
EXPECTED_EMPTY_GROUPS = 3


def test_player_search_includes_club_membership_players(client: Client) -> None:
    """Club membership alone should make a player searchable."""
    tracker = create_tracker_match(prefix="Membership Search")
    member = create_tracker_player(username="member_player")
    connect_user_to_match_club(member.user, tracker.home_team.club, tracker.match)
    login_home_club_editor(client, tracker, "roster_editor")

    response = client.get(
        f"/api/match/player_search/{tracker.match.id_uuid}/"
        f"{tracker.home_team.id_uuid}/?search=member",
    )

    assert response.status_code == HTTPStatus.OK
    assert {player["user"]["username"] for player in response.json()["players"]} == {
        "member_player",
    }


@pytest.mark.parametrize("search_term", ["joelle", "joele"])
def test_player_search_matches_accent_and_typo_variants(
    client: Client,
    search_term: str,
) -> None:
    """Search should tolerate accents and a close typo in a player's name."""
    tracker = create_tracker_match(prefix=f"Name Search {search_term}")
    player = create_tracker_player(username="Joëlle")
    connect_user_to_match_club(player.user, tracker.home_team.club, tracker.match)
    login_home_club_editor(client, tracker, "name_variant_editor")

    response = client.get(
        f"/api/match/player_search/{tracker.match.id_uuid}/"
        f"{tracker.home_team.id_uuid}/?search={search_term}",
    )

    assert response.status_code == HTTPStatus.OK
    assert {player["user"]["username"] for player in response.json()["players"]} == {
        "Joëlle",
    }


def test_player_search_includes_other_team_players_from_same_club(
    client: Client,
) -> None:
    """Season roster players from another team in the club should be visible."""
    tracker = create_tracker_match(prefix="Same Club Search")
    other_team = Team.objects.create(
        name="Same Club Other Team",
        club=tracker.home_team.club,
    )
    other_team_data = TeamData.objects.create(
        team=other_team,
        season=tracker.match.season,
    )
    other_team_data.players.add(create_tracker_player(username="clubmate_player"))
    login_home_club_editor(client, tracker, "same_club_search_editor")

    response = client.get(
        f"/api/match/player_search/{tracker.match.id_uuid}/"
        f"{tracker.home_team.id_uuid}/?search=clubmate",
    )

    assert response.status_code == HTTPStatus.OK
    assert {player["user"]["username"] for player in response.json()["players"]} == {
        "clubmate_player",
    }


def test_player_search_keeps_candidates_when_groups_are_empty(client: Client) -> None:
    """Empty groups must not produce a NOT IN NULL exclusion."""
    tracker = create_tracker_match(prefix="Empty Groups Search")
    create_group_types("Aanval", "Verdediging", "Reserve")
    login_home_club_editor(client, tracker, "empty_groups_search_editor")

    overview_response = client.get(
        f"/api/match/player_overview_data/{tracker.match.id_uuid}/"
        f"{tracker.home_team.id_uuid}/",
    )
    assert overview_response.status_code == HTTPStatus.OK
    empty_groups = PlayerGroup.objects.filter(
        match_data=tracker.match_data,
        team=tracker.home_team,
        players__isnull=True,
    )
    assert empty_groups.count() == EXPECTED_EMPTY_GROUPS

    candidate = create_tracker_player(username="daan_candidate")
    connect_user_to_match_club(
        candidate.user,
        tracker.home_team.club,
        tracker.match,
    )
    response = client.get(
        f"/api/match/player_search/{tracker.match.id_uuid}/"
        f"{tracker.home_team.id_uuid}/?search=daan",
    )

    assert response.status_code == HTTPStatus.OK
    assert {player["user"]["username"] for player in response.json()["players"]} == {
        "daan_candidate",
    }


def test_player_search_rejects_non_club_users(client: Client) -> None:
    """Unrelated authenticated users must not see a club roster."""
    tracker = create_tracker_match(prefix="Outsider Search")
    client.force_login(create_tracker_user(username="player_search_outsider"))

    response = client.get(
        f"/api/match/player_search/{tracker.match.id_uuid}/"
        f"{tracker.home_team.id_uuid}/?search=member",
    )

    assert response.status_code == HTTPStatus.FORBIDDEN
    assert response.json() == {
        "error": "You do not have permission to edit player groups.",
    }


def test_player_search_does_not_match_email_addresses(client: Client) -> None:
    """Private email content must not be a searchable player identifier."""
    tracker = create_tracker_match(prefix="Private Email Search")
    player = create_tracker_player(
        username="visible_username",
        email="private-token@example.com",
    )
    connect_user_to_match_club(player.user, tracker.home_team.club, tracker.match)
    login_home_club_editor(client, tracker, "private_email_search_editor")

    response = client.get(
        f"/api/match/player_search/{tracker.match.id_uuid}/"
        f"{tracker.home_team.id_uuid}/?search=private-token",
    )

    assert response.status_code == HTTPStatus.OK
    assert response.json() == {"players": []}
