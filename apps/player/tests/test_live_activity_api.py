"""Live Activity registration endpoint contracts."""

from __future__ import annotations

from http import HTTPStatus
import json
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.test.client import Client
import pytest

from apps.player.models.live_activity import MatchLiveActivity
from apps.schedule.tests.match_api_test_support import create_match_graph


URL = "/api/player/me/live-activities/"
TOKEN = "0123456789abcdef" * 4


def post(client: Client, payload: dict) -> object:
    """Register an activity as JSON."""
    return client.post(URL, data=json.dumps(payload), content_type="application/json")


def delete(client: Client, payload: dict) -> object:
    """End an activity as JSON."""
    return client.delete(URL, data=json.dumps(payload), content_type="application/json")


@pytest.mark.django_db
def test_live_activities_require_authentication(client: Client) -> None:
    """Anonymous phones cannot register activity tokens."""
    response = post(client, {"match_id": str(uuid4()), "push_token": TOKEN})
    assert response.status_code in {HTTPStatus.FORBIDDEN, HTTPStatus.UNAUTHORIZED}


@pytest.mark.django_db
def test_register_refresh_and_end_live_activity(client: Client) -> None:
    """Registering twice upserts; ending is owner-scoped and idempotent."""
    graph = create_match_graph(prefix="Live activity API")
    users = get_user_model()
    owner = users.objects.create_user(username="la-api-owner")
    other = users.objects.create_user(username="la-api-other")
    client.force_login(owner)
    match_id = str(graph.match.pk)

    created = post(client, {"match_id": match_id, "push_token": TOKEN})
    assert created.status_code == HTTPStatus.CREATED
    assert created.json()["created"] is True
    refreshed = post(client, {"match_id": match_id, "push_token": TOKEN})
    assert refreshed.status_code == HTTPStatus.OK
    assert refreshed.json()["created"] is False
    assert MatchLiveActivity.objects.filter(user=owner, is_active=True).count() == 1

    assert post(
        client, {"match_id": match_id, "push_token": "not hex"}
    ).status_code == (HTTPStatus.BAD_REQUEST)
    assert post(
        client, {"match_id": str(uuid4()), "push_token": TOKEN}
    ).status_code == (HTTPStatus.NOT_FOUND)

    client.force_login(other)
    assert delete(client, {"push_token": TOKEN}).status_code == HTTPStatus.NOT_FOUND
    client.force_login(owner)
    assert delete(client, {"push_token": TOKEN}).status_code == HTTPStatus.NO_CONTENT
    assert delete(client, {"push_token": TOKEN}).status_code == HTTPStatus.NO_CONTENT
    assert not MatchLiveActivity.objects.get(push_token=TOKEN).is_active
