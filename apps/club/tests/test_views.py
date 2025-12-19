"""Tests for club views."""

from django.contrib.auth import get_user_model
from django.test.client import Client
import pytest

from apps.club.models import Club


TEST_PASSWORD = "pass1234"  # noqa: S105  # nosec B105 - test credential constant
HTTP_STATUS_OK = 200


@pytest.mark.django_db
def test_club_detail_allows_authenticated_spectator(client: Client) -> None:
    """Ensure a logged-in user without a Player profile can view club data.

    The legacy Django-rendered club detail view was removed when the project
    migrated to a React SPA. This test now targets the API endpoint that powers
    the SPA club detail page.

    """
    user = get_user_model().objects.create_user(
        username="viewer",
        password=TEST_PASSWORD,
    )
    club = Club.objects.create(name="Viewing Club")

    client.force_login(user)

    response = client.get(f"/api/club/clubs/{club.id_uuid}/overview/", secure=True)

    assert response.status_code == HTTP_STATUS_OK
    payload = response.json()
    assert payload["club"]["id_uuid"] == str(club.id_uuid)
    assert payload["club"]["name"] == club.name
