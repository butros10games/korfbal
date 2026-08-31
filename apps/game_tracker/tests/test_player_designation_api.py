"""Focused contracts for assigning players to match groups."""

from http import HTTPStatus

from django.test.client import Client
import pytest

from apps.game_tracker.models import MatchPlayer
from apps.game_tracker.tests.tracker_test_helpers import (
    create_group_types,
    create_tracker_match,
    create_tracker_player,
    create_tracker_user,
    get_tracker_group,
    login_home_club_editor,
)


pytestmark = pytest.mark.django_db

DESIGNATION_URL = "/api/match/player_designation/"


def test_reserve_accepts_exactly_sixteen_players(client: Client) -> None:
    """Reserve accepts the documented maximum and advances the revision."""
    tracker = create_tracker_match(prefix="Reserve boundary")
    create_group_types("Reserve")
    reserve = get_tracker_group(tracker, "Reserve")
    login_home_club_editor(client, tracker, "reserve-boundary-editor")
    players = [
        create_tracker_player(username=f"reserve-boundary-{index}")
        for index in range(16)
    ]
    expected_revision = tracker.match_data.live_revision

    response = client.post(
        DESIGNATION_URL,
        data={
            "new_group_id": str(reserve.id_uuid),
            "players": [{"id_uuid": str(player.id_uuid)} for player in players],
            "expected_revision": expected_revision,
        },
        content_type="application/json",
    )

    assert response.status_code == HTTPStatus.OK
    assert response.json() == {
        "success": True,
        "live_revision": expected_revision + 1,
    }
    assert set(reserve.players.values_list("id_uuid", flat=True)) == {
        player.id_uuid for player in players
    }


@pytest.mark.parametrize(
    ("group_name", "initial_count", "added_count"),
    [
        pytest.param("Reserve", 10, 7, id="reserve-total-over-16"),
        pytest.param("Aanval", 0, 5, id="non-reserve-over-4"),
    ],
)
def test_designation_rejects_group_capacity_overflow_without_mutation(
    client: Client,
    group_name: str,
    initial_count: int,
    added_count: int,
) -> None:
    """Neither reserve nor court groups may exceed their capacity."""
    tracker = create_tracker_match(prefix=f"{group_name} overflow")
    create_group_types(group_name)
    group = get_tracker_group(tracker, group_name)
    login_home_club_editor(client, tracker, f"{group_name}-overflow-editor")
    initial_players = [
        create_tracker_player(username=f"{group_name}-initial-{index}")
        for index in range(initial_count)
    ]
    group.players.add(*initial_players)
    new_players = [
        create_tracker_player(username=f"{group_name}-added-{index}")
        for index in range(added_count)
    ]
    expected_revision = tracker.match_data.live_revision

    response = client.post(
        DESIGNATION_URL,
        data={
            "new_group_id": str(group.id_uuid),
            "players": [{"id_uuid": str(player.id_uuid)} for player in new_players],
            "expected_revision": expected_revision,
        },
        content_type="application/json",
    )

    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert response.json() == {"error": "Too many players selected"}
    assert set(group.players.values_list("id_uuid", flat=True)) == {
        player.id_uuid for player in initial_players
    }
    assert not MatchPlayer.objects.filter(match_data=tracker.match_data).exists()
    tracker.match_data.refresh_from_db()
    assert tracker.match_data.live_revision == expected_revision


def test_non_reserve_move_requires_reserve_source(client: Client) -> None:
    """A court player cannot move directly into another court group."""
    tracker = create_tracker_match(prefix="Reserve source")
    create_group_types("Aanval", "Verdediging", "Reserve")
    reserve = get_tracker_group(tracker, "Reserve")
    attack = get_tracker_group(tracker, "Aanval")
    defense = get_tracker_group(tracker, "Verdediging")
    player = create_tracker_player(username="reserve-source-player")
    reserve.players.add(player)
    login_home_club_editor(client, tracker, "reserve-source-editor")

    first_move = client.post(
        DESIGNATION_URL,
        data={
            "new_group_id": str(attack.id_uuid),
            "players": [
                {
                    "id_uuid": str(player.id_uuid),
                    "groupId": str(reserve.id_uuid),
                }
            ],
            "expected_revision": tracker.match_data.live_revision,
        },
        content_type="application/json",
    )
    assert first_move.status_code == HTTPStatus.OK

    rejected_move = client.post(
        DESIGNATION_URL,
        data={
            "new_group_id": str(defense.id_uuid),
            "players": [
                {
                    "id_uuid": str(player.id_uuid),
                    "groupId": str(attack.id_uuid),
                }
            ],
            "expected_revision": first_move.json()["live_revision"],
        },
        content_type="application/json",
    )

    assert rejected_move.status_code == HTTPStatus.BAD_REQUEST
    assert rejected_move.json() == {
        "error": f"{player} is not in the reserve player group."
    }
    assert attack.players.filter(pk=player.pk).exists()
    assert not reserve.players.filter(pk=player.pk).exists()
    assert not defense.players.filter(pk=player.pk).exists()


def test_designation_syncs_match_player_through_final_group_removal(
    client: Client,
) -> None:
    """The roster follows assignment and final-group removal."""
    tracker = create_tracker_match(prefix="Roster sync")
    create_group_types("Reserve")
    reserve = get_tracker_group(tracker, "Reserve")
    login_home_club_editor(client, tracker, "roster-sync-editor")
    players = [
        create_tracker_player(username=f"roster-sync-{index}") for index in range(2)
    ]

    added = client.post(
        DESIGNATION_URL,
        data={
            "new_group_id": str(reserve.id_uuid),
            "players": [{"id_uuid": str(player.id_uuid)} for player in players],
            "expected_revision": tracker.match_data.live_revision,
        },
        content_type="application/json",
    )
    assert added.status_code == HTTPStatus.OK
    roster = MatchPlayer.objects.filter(
        match_data=tracker.match_data,
        team=tracker.home_team,
    )
    assert set(roster.values_list("player_id", flat=True)) == {
        player.id_uuid for player in players
    }

    removed = client.post(
        DESIGNATION_URL,
        data={
            "new_group_id": None,
            "players": [
                {
                    "id_uuid": str(players[0].id_uuid),
                    "groupId": str(reserve.id_uuid),
                }
            ],
            "expected_revision": added.json()["live_revision"],
        },
        content_type="application/json",
    )

    assert removed.status_code == HTTPStatus.OK
    assert set(roster.values_list("player_id", flat=True)) == {players[1].id_uuid}


def test_stale_revision_keeps_only_accepted_membership(client: Client) -> None:
    """A stale update cannot overwrite an accepted designation."""
    tracker = create_tracker_match(prefix="Designation conflict")
    create_group_types("Reserve")
    reserve = get_tracker_group(tracker, "Reserve")
    login_home_club_editor(client, tracker, "designation-conflict-editor")
    accepted_player = create_tracker_player(username="designation-accepted")
    stale_player = create_tracker_player(username="designation-stale")
    expected_revision = tracker.match_data.live_revision

    accepted = client.post(
        DESIGNATION_URL,
        data={
            "new_group_id": str(reserve.id_uuid),
            "players": [{"id_uuid": str(accepted_player.id_uuid)}],
            "expected_revision": expected_revision,
        },
        content_type="application/json",
    )
    assert accepted.status_code == HTTPStatus.OK

    stale = client.post(
        DESIGNATION_URL,
        data={
            "new_group_id": str(reserve.id_uuid),
            "players": [{"id_uuid": str(stale_player.id_uuid)}],
            "expected_revision": expected_revision,
        },
        content_type="application/json",
    )

    assert stale.status_code == HTTPStatus.CONFLICT
    assert stale.json() == {
        "code": "revision_conflict",
        "detail": "The match changed while you were editing.",
        "expected_revision": expected_revision,
        "live_revision": accepted.json()["live_revision"],
    }
    assert set(reserve.players.values_list("id_uuid", flat=True)) == {
        accepted_player.id_uuid
    }


def test_outsider_cannot_designate_players(client: Client) -> None:
    """An authenticated outsider cannot change groups or the revision."""
    tracker = create_tracker_match(prefix="Designation outsider")
    create_group_types("Reserve")
    reserve = get_tracker_group(tracker, "Reserve")
    client.force_login(create_tracker_user(username="designation-outsider"))
    player = create_tracker_player(username="designation-candidate")
    expected_revision = tracker.match_data.live_revision

    response = client.post(
        DESIGNATION_URL,
        data={
            "new_group_id": str(reserve.id_uuid),
            "players": [{"id_uuid": str(player.id_uuid)}],
            "expected_revision": expected_revision,
        },
        content_type="application/json",
    )

    assert response.status_code == HTTPStatus.FORBIDDEN
    assert response.json() == {
        "error": "You do not have permission to edit player groups."
    }
    assert not reserve.players.exists()
    assert not MatchPlayer.objects.filter(match_data=tracker.match_data).exists()
    tracker.match_data.refresh_from_db()
    assert tracker.match_data.live_revision == expected_revision
