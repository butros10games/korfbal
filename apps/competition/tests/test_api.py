"""Catalogue API authentication and filter regression tests."""

from __future__ import annotations

from datetime import date

from django.contrib.auth import get_user_model
import pytest
from rest_framework import status
from rest_framework.test import APIClient

from apps.competition.models import Club
from apps.schedule.models import Season


@pytest.mark.django_db
def test_catalogue_requires_json_auth() -> None:
    """Unauthenticated clients receive an API error, never a login redirect."""
    response = APIClient().get("/api/competition/clubs/")
    assert response.status_code in {401, 403}
    assert "application/json" in response["Content-Type"]


@pytest.mark.django_db
def test_catalogue_is_paginated_searchable_and_read_only() -> None:
    """Serve bounded local reads and reject writes even for logged-in users."""
    user = get_user_model().objects.create_user(username="catalogue")
    client = APIClient()
    client.force_authenticate(user)
    Club.objects.create(external_id="C1", name="Example Club")
    response = client.get("/api/competition/clubs/?search=Example&page_size=1")
    assert response.status_code == status.HTTP_200_OK
    assert response.data["count"] == 1
    assert response.data["results"][0]["name"] == "Example Club"
    assert (
        client.post("/api/competition/clubs/", {}).status_code
        == status.HTTP_405_METHOD_NOT_ALLOWED
    )
    assert (
        client.get("/api/competition/teams/?season=invalid").status_code
        == status.HTTP_400_BAD_REQUEST
    )


@pytest.mark.django_db
def test_season_api_exposes_filter_identifiers() -> None:
    """Clients can discover season IDs without out-of-band database access."""
    season = Season.objects.create(
        name="2026", start_date=date(2026, 7, 1), end_date=date(2027, 6, 30)
    )
    client = APIClient()
    client.force_authenticate(get_user_model().objects.create_user(username="seasons"))
    response = client.get("/api/competition/seasons/")
    assert response.status_code == status.HTTP_200_OK
    assert response.data["results"][0]["id_uuid"] == str(season.pk)
