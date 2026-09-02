"""Pool-first and match-first tournament administration tests."""

from __future__ import annotations

from http import HTTPStatus

from django.contrib.auth import get_user_model
from django.test import Client
from django.utils import timezone
import pytest

from apps.tournament.models import Tournament, TournamentField, TournamentTeam


pytestmark = pytest.mark.django_db


def _setup(client: Client) -> tuple[Tournament, list[TournamentTeam]]:
    owner = get_user_model().objects.create_user(username="planning-manager")
    client.force_login(owner)
    tournament = Tournament.objects.create(
        name="Planning admin",
        slug="planning-admin",
        owner=owner,
        starts_at=timezone.now(),
        timezone="Europe/Amsterdam",
    )
    teams = [
        TournamentTeam.objects.create(
            tournament=tournament,
            name=f"Team {index}",
            seed=index,
        )
        for index in range(1, 5)
    ]
    TournamentField.objects.create(tournament=tournament, label="Veld 1")
    return tournament, teams


def test_pool_and_match_generation_are_separate_review_steps(client: Client) -> None:
    """Generating pools leaves them editable until matches are generated separately."""
    tournament, _ = _setup(client)
    expected_count = 2

    pools_response = client.post(
        f"/api/tournaments/{tournament.id_uuid}/pools/generate/",
        data={"pool_count": 2, "strategy": "snake"},
        content_type="application/json",
    )

    assert pools_response.status_code == HTTPStatus.OK
    assert tournament.pools.count() == expected_count
    assert tournament.matches.count() == 0
    pool_ids = set(tournament.pools.values_list("id_uuid", flat=True))

    matches_response = client.post(
        f"/api/tournaments/{tournament.id_uuid}/matches/generate/",
        data={"legs": 1},
        content_type="application/json",
    )

    assert matches_response.status_code == HTTPStatus.OK
    assert tournament.matches.count() == expected_count
    assert set(tournament.pools.values_list("id_uuid", flat=True)) == pool_ids


def test_manual_pool_and_match_creation_feed_the_same_snapshot(client: Client) -> None:
    """Organizer-created records use the normal snapshot and remain editable."""
    tournament, teams = _setup(client)
    field_id = str(tournament.fields.get().id_uuid)
    pool_response = client.post(
        f"/api/tournaments/{tournament.id_uuid}/pools/",
        data={
            "name": "Poule Handmatig",
            "assigned_field_id": field_id,
            "team_ids": [str(teams[0].id_uuid), str(teams[1].id_uuid)],
        },
        content_type="application/json",
    )

    assert pool_response.status_code == HTTPStatus.CREATED
    assert pool_response.json()["pools"][0]["assigned_field"] == {
        "id_uuid": field_id,
        "label": "Veld 1",
    }
    pool_id = pool_response.json()["pools"][0]["id_uuid"]
    create_response = client.post(
        f"/api/tournaments/{tournament.id_uuid}/matches/",
        data={
            "pool_id": pool_id,
            "home_team_id": str(teams[0].id_uuid),
            "away_team_id": str(teams[1].id_uuid),
            "field_id": field_id,
            "date": "2027-06-12",
            "start_time": "09:00",
            "duration_minutes": 20,
            "round_number": 1,
        },
        content_type="application/json",
    )

    assert create_response.status_code == HTTPStatus.CREATED
    match = tournament.matches.get()
    update_response = client.patch(
        f"/api/tournaments/{tournament.id_uuid}/matches/{match.id_uuid}/",
        data={"start_time": "09:30"},
        content_type="application/json",
    )

    assert update_response.status_code == HTTPStatus.OK
    match.refresh_from_db()
    assert match.starts_at.isoformat() == "2027-06-12T07:30:00+00:00"

    delete_response = client.delete(
        f"/api/tournaments/{tournament.id_uuid}/matches/{match.id_uuid}/"
    )
    assert delete_response.status_code == HTTPStatus.NO_CONTENT

    update_pool_response = client.patch(
        f"/api/tournaments/{tournament.id_uuid}/pools/{pool_id}/",
        data={"team_ids": [str(teams[0].id_uuid), str(teams[2].id_uuid)]},
        content_type="application/json",
    )
    assert update_pool_response.status_code == HTTPStatus.OK
    assert (
        update_pool_response.json()["pools"][0]["assigned_field"]["id_uuid"] == field_id
    )
    assert {
        row["team_id"] for row in update_pool_response.json()["pools"][0]["standings"]
    } == {str(teams[0].id_uuid), str(teams[2].id_uuid)}


def test_manual_match_must_use_the_pool_assigned_field(client: Client) -> None:
    """Manual planning cannot silently violate a pool's fixed field."""
    tournament, teams = _setup(client)
    assigned_field = tournament.fields.get()
    other_field = TournamentField.objects.create(
        tournament=tournament,
        label="Veld 2",
    )
    pool_response = client.post(
        f"/api/tournaments/{tournament.id_uuid}/pools/",
        data={
            "name": "Poule A",
            "assigned_field_id": str(assigned_field.id_uuid),
            "team_ids": [str(team.id_uuid) for team in teams],
        },
        content_type="application/json",
    )

    response = client.post(
        f"/api/tournaments/{tournament.id_uuid}/matches/",
        data={
            "pool_id": pool_response.json()["pools"][0]["id_uuid"],
            "home_team_id": str(teams[0].id_uuid),
            "away_team_id": str(teams[1].id_uuid),
            "field_id": str(other_field.id_uuid),
            "date": "2027-06-12",
            "start_time": "09:00",
            "duration_minutes": 20,
            "round_number": 1,
        },
        content_type="application/json",
    )

    assert response.status_code == HTTPStatus.CONFLICT
    assert "assigned to field" in response.json()["detail"]
