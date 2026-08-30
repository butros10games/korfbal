"""Regression tests for malformed UUIDs at schedule API boundaries."""

from __future__ import annotations

from http import HTTPStatus
from typing import Any, cast

from django.contrib.auth import get_user_model
from django.test.client import Client
import pytest


pytestmark = pytest.mark.django_db


@pytest.mark.parametrize("endpoint", ["", "next/", "upcoming/", "recent/"])
@pytest.mark.parametrize("parameter", ["team", "club", "season"])
def test_match_reads_reject_malformed_uuid_filters(
    client: Client,
    endpoint: str,
    parameter: str,
) -> None:
    """Every match read backed by the shared queryset validates UUID filters."""
    response = client.get(f"/api/matches/{endpoint}", {parameter: "not-a-uuid"})

    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert parameter in response.json()


@pytest.mark.parametrize("parameter", ["team", "club", "season"])
def test_finished_matches_reject_malformed_uuid_filters(
    client: Client,
    parameter: str,
) -> None:
    """The specialized finished query validates its UUID filters too."""
    response = client.get(
        "/api/matches/finished/",
        {parameter: "not-a-uuid"},
    )

    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert parameter in response.json()


def test_season_pool_list_rejects_malformed_uuid_filter(client: Client) -> None:
    """Pool filtering reports malformed season IDs as client errors."""
    user_model = cast(Any, get_user_model())
    staff = user_model.objects.create_user(
        username="pool_uuid_validation_staff",
        is_staff=True,
    )
    client.force_login(staff)

    response = client.get("/api/seasons/pools/", {"season": "not-a-uuid"})

    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert "season" in response.json()


@pytest.mark.parametrize(
    "path",
    [
        "/api/matches/not-a-uuid/",
        "/api/seasons/not-a-uuid/",
        "/api/seasons/pools/not-a-uuid/",
    ],
)
def test_schedule_detail_routes_reject_malformed_uuid_paths(
    client: Client,
    path: str,
) -> None:
    """Malformed schedule resource IDs do not reach ORM UUID coercion."""
    response = client.get(path)

    assert response.status_code == HTTPStatus.NOT_FOUND
