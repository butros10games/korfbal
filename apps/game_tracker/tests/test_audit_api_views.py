"""Audit coverage for game-tracker API authentication and payload boundaries."""

from __future__ import annotations

from http import HTTPStatus
import json

from django.test.client import Client
import pytest


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("method", "url"),
    [
        (
            "get",
            "/api/match/player_search/00000000-0000-0000-0000-000000000001/00000000-0000-0000-0000-000000000002/?search=player",
        ),
        ("post", "/api/match/player_designation/"),
    ],
)
def test_private_player_group_endpoints_reject_anonymous_requests_as_json(
    client: Client,
    method: str,
    url: str,
) -> None:
    """API authentication failures never redirect clients to an HTML login page."""
    response = getattr(client, method)(
        url,
        data={} if method == "post" else None,
        content_type="application/json" if method == "post" else None,
        secure=True,
    )

    assert response.status_code == HTTPStatus.UNAUTHORIZED
    assert response.headers["Content-Type"].startswith("application/json")


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ([], {"error": "Invalid JSON data"}),
        ({}, {"error": "No player selected"}),
        ({"players": "not-a-list"}, {"error": "No player selected"}),
        ({"players": ["not-an-object"]}, {"error": "No player selected"}),
        ({"players": [{}]}, {"error": "Invalid player"}),
        (
            {"players": [{"id_uuid": "player-id"}], "new_group_id": 42},
            {"error": "Unknown player group"},
        ),
        (
            {"players": [{"id_uuid": "player-id"}]},
            {"expected_revision": ["A non-negative integer is required."]},
        ),
        (
            {
                "players": [{"id_uuid": "player-id"}],
                "expected_revision": True,
            },
            {"expected_revision": ["A non-negative integer is required."]},
        ),
        (
            {
                "players": [{"id_uuid": "player-id"}],
                "expected_revision": -1,
            },
            {"expected_revision": ["A non-negative integer is required."]},
        ),
    ],
)
def test_player_designation_rejects_malformed_payloads_before_application_call(
    admin_client: Client,
    payload: object,
    expected: dict[str, object],
) -> None:
    """Object shape, selection identity, target, and revision are fail-closed."""
    response = admin_client.post(
        "/api/match/player_designation/",
        data=json.dumps(payload),
        content_type="application/json",
        secure=True,
    )

    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert response.json() == expected


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("search", "expected_error"),
    [
        ("", {"error": "No player selected"}),
        (
            "ab",
            {
                "success": False,
                "error": "Player name should be at least 3 characters long",
            },
        ),
        (
            "x" * 51,
            {
                "success": False,
                "error": "Player name should be at most 50 characters long",
            },
        ),
    ],
)
def test_player_search_validates_query_before_loading_match_data(
    admin_client: Client,
    search: str,
    expected_error: dict[str, object],
) -> None:
    """Invalid search input has a stable response independent of match existence."""
    response = admin_client.get(
        "/api/match/player_search/"
        "00000000-0000-0000-0000-000000000001/"
        f"00000000-0000-0000-0000-000000000002/?search={search}",
        secure=True,
    )

    expected_status = HTTPStatus.BAD_REQUEST if not search else HTTPStatus.OK
    assert response.status_code == expected_status
    assert response.json() == expected_error
