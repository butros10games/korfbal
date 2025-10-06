"""Tests for hub views and APIs."""

import json

from django.contrib.auth import get_user_model
from django.test.client import Client
from django.urls import reverse
import pytest


TEST_PASSWORD = "pass1234"  # nosonar # noqa: S105
HTTP_STATUS_OK = 200


@pytest.mark.django_db
def test_hub_index_allows_authenticated_spectator(client: Client) -> None:
    """Ensure hub index loads for users without a Player profile."""
    user = get_user_model().objects.create_user(
        username="spectator",
        password=TEST_PASSWORD,
    )
    client.force_login(user)

    response = client.get(reverse("index"), secure=True)

    assert response.status_code == HTTP_STATUS_OK
    assert response.context["match"] is None
    assert response.context["match_data"] is None


@pytest.mark.django_db
def test_catalog_data_returns_empty_lists_for_spectator(client: Client) -> None:
    """Ensure catalog data API responds gracefully without a Player profile."""
    user = get_user_model().objects.create_user(
        username="spectator2",
        password=TEST_PASSWORD,
    )
    client.force_login(user)

    response = client.post(
        reverse("api_catalog_data"),
        data=json.dumps({"value": "teams"}),
        content_type="application/json",
        secure=True,
    )

    assert response.status_code == HTTP_STATUS_OK
    payload = response.json()
    assert payload["type"] == "teams"
    assert payload["connected"] == []
    assert payload["following"] == []
