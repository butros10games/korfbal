"""Tests for hub views and APIs."""

import json

from django.contrib.auth import get_user_model
from django.urls import reverse
import pytest


@pytest.mark.django_db
def test_hub_index_allows_authenticated_spectator(client) -> None:
    """Ensure hub index loads for users without a Player profile."""
    user = get_user_model().objects.create_user(
        username="spectator",
        password="pass1234",
    )
    client.force_login(user)

    response = client.get(reverse("index"))

    assert response.status_code == 200
    assert response.context["match"] is None
    assert response.context["match_data"] is None


@pytest.mark.django_db
def test_catalog_data_returns_empty_lists_for_spectator(client) -> None:
    """Ensure catalog data API responds gracefully without a Player profile."""
    user = get_user_model().objects.create_user(
        username="spectator2",
        password="pass1234",
    )
    client.force_login(user)

    response = client.post(
        reverse("api_catalog_data"),
        data=json.dumps({"value": "teams"}),
        content_type="application/json",
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["type"] == "teams"
    assert payload["connected"] == []
    assert payload["following"] == []
