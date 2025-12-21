"""Tests for team CRUD permission boundaries."""

from __future__ import annotations

from http import HTTPStatus

from django.contrib.auth import get_user_model
from django.test import override_settings
from django.test.client import Client
import pytest

from apps.club.models import Club
from apps.team.models import Team


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_team_crud_requires_staff_for_write_operations(client: Client) -> None:
    """Non-staff users should not be able to create/update/delete teams."""
    club = Club.objects.create(name="Club")

    non_staff = get_user_model().objects.create_user(
        username="non_staff",
        password="pass1234",  # noqa: S106  # nosec
    )

    client.force_login(non_staff)

    response_create = client.post(
        "/api/team/teams/",
        data={"name": "New Team", "club_id": str(club.id_uuid)},
        content_type="application/json",
    )
    assert response_create.status_code == HTTPStatus.FORBIDDEN

    team = Team.objects.create(name="Existing", club=club)

    response_patch = client.patch(
        f"/api/team/teams/{team.id_uuid}/",
        data={"name": "Changed"},
        content_type="application/json",
    )
    assert response_patch.status_code == HTTPStatus.FORBIDDEN

    response_delete = client.delete(f"/api/team/teams/{team.id_uuid}/")
    assert response_delete.status_code == HTTPStatus.FORBIDDEN


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_team_crud_allows_staff_user(client: Client) -> None:
    """Staff users can manage teams (admin tooling / data migrations)."""
    club = Club.objects.create(name="Club")

    staff = get_user_model().objects.create_user(
        username="staff",
        password="pass1234",  # noqa: S106  # nosec
        is_staff=True,
    )

    client.force_login(staff)

    response_create = client.post(
        "/api/team/teams/",
        data={"name": "New Team", "club_id": str(club.id_uuid)},
        content_type="application/json",
    )
    assert response_create.status_code == HTTPStatus.CREATED

    created_id = response_create.json()["id_uuid"]

    response_patch = client.patch(
        f"/api/team/teams/{created_id}/",
        data={"name": "Renamed"},
        content_type="application/json",
    )
    assert response_patch.status_code == HTTPStatus.OK
    assert response_patch.json()["name"] == "Renamed"

    response_delete = client.delete(f"/api/team/teams/{created_id}/")
    assert response_delete.status_code == HTTPStatus.NO_CONTENT
