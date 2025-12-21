"""Tests for club CRUD permission boundaries."""

from __future__ import annotations

from http import HTTPStatus

from django.contrib.auth import get_user_model
from django.test import override_settings
from django.test.client import Client
import pytest

from apps.club.models import Club


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_club_list_and_detail_are_public(client: Client) -> None:
    """Anonymous users should be able to browse clubs."""
    club = Club.objects.create(name="Public Club")

    response_list = client.get("/api/club/clubs/")
    assert response_list.status_code == HTTPStatus.OK

    response_detail = client.get(f"/api/club/clubs/{club.id_uuid}/")
    assert response_detail.status_code == HTTPStatus.OK
    assert response_detail.json()["id_uuid"] == str(club.id_uuid)


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_club_crud_requires_staff_for_write_operations(client: Client) -> None:
    """Non-staff users should not be able to create/update/delete clubs."""
    non_staff = get_user_model().objects.create_user(
        username="non_staff",
        password="pass1234",  # noqa: S106  # nosec
    )

    client.force_login(non_staff)

    response_create = client.post(
        "/api/club/clubs/",
        data={"name": "New Club"},
        content_type="application/json",
    )
    assert response_create.status_code == HTTPStatus.FORBIDDEN

    club = Club.objects.create(name="Existing")

    response_patch = client.patch(
        f"/api/club/clubs/{club.id_uuid}/",
        data={"name": "Renamed"},
        content_type="application/json",
    )
    assert response_patch.status_code == HTTPStatus.FORBIDDEN

    response_delete = client.delete(f"/api/club/clubs/{club.id_uuid}/")
    assert response_delete.status_code == HTTPStatus.FORBIDDEN


@pytest.mark.django_db
@override_settings(SECURE_SSL_REDIRECT=False)
def test_club_crud_allows_staff_user(client: Client) -> None:
    """Staff users can manage clubs (admin tooling / data migrations)."""
    staff = get_user_model().objects.create_user(
        username="staff",
        password="pass1234",  # noqa: S106  # nosec
        is_staff=True,
    )

    client.force_login(staff)

    response_create = client.post(
        "/api/club/clubs/",
        data={"name": "New Club"},
        content_type="application/json",
    )
    assert response_create.status_code == HTTPStatus.CREATED

    created_id = response_create.json()["id_uuid"]

    response_patch = client.patch(
        f"/api/club/clubs/{created_id}/",
        data={"name": "Renamed"},
        content_type="application/json",
    )
    assert response_patch.status_code == HTTPStatus.OK
    assert response_patch.json()["name"] == "Renamed"

    response_delete = client.delete(f"/api/club/clubs/{created_id}/")
    assert response_delete.status_code == HTTPStatus.NO_CONTENT
