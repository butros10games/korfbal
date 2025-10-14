"""Tests for club views."""

from django.contrib.auth import get_user_model
from django.test.client import Client
from django.urls import reverse
import pytest

from apps.club.models import Club


TEST_PASSWORD = "pass1234"  # noqa: S105  # nosec B105 - test credential constant
HTTP_STATUS_OK = 200


@pytest.mark.django_db
def test_club_detail_allows_authenticated_spectator(client: Client) -> None:
    """Ensure a logged-in user without a Player profile can view club detail."""
    user = get_user_model().objects.create_user(
        username="viewer",
        password=TEST_PASSWORD,
    )
    club = Club.objects.create(name="Viewing Club")

    client.force_login(user)

    response = client.get(reverse("club_detail", args=[club.id_uuid]), secure=True)

    assert response.status_code == HTTP_STATUS_OK
    assert response.context["admin"] is False
    assert response.context["following"] is False
