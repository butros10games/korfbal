"""Cached public reads preserve security, filtering and response contracts."""

from http import HTTPStatus
from unittest.mock import patch

from django.conf import settings
from django.db import connection
from django.test import Client, override_settings
from django.test.utils import CaptureQueriesContext
import pytest

from apps.game_tracker.composition import publish_public_live_snapshot
from apps.schedule.tests.match_api_test_support import create_match_graph


pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def live_route() -> str:
    """Publish synthetic committed state before issuing requests."""
    graph = create_match_graph(prefix="Cache middleware")
    publish_public_live_snapshot(match_id=str(graph.match.pk))
    return f"/api/matches/{graph.match.pk}/live/"


@pytest.mark.parametrize(
    "suffix", ["", "poll/?since_revision=-1", "poll/?since_revision=0&timeout=25"]
)
def test_cached_response_matches_drf(
    client: Client, live_route: str, suffix: str
) -> None:
    """The shortcut shares serialization and security headers with the API."""
    route = live_route + suffix
    with CaptureQueriesContext(connection) as queries:
        response = client.get(route)
    assert not queries
    assert response["X-Korfbal-Live-Cache"] == "hit"
    with patch(
        "apps.schedule.api.public_live_cache.read_cached_live", return_value=None
    ):
        expected = client.get(route)
    actual_payload, expected_payload = response.json(), expected.json()
    actual_payload.pop("server_time", None)
    expected_payload.pop("server_time", None)
    assert actual_payload == expected_payload
    for header in (
        "Content-Type",
        "Allow",
        "X-Content-Type-Options",
        "X-Frame-Options",
    ):
        assert response.get(header) == expected.get(header)
    assert set(response.get("Vary", "").split(", ")) == set(
        expected.get("Vary", "").split(", ")
    )


@pytest.mark.parametrize(
    ("suffix", "headers"),
    [
        ("?team=unknown", {}),
        ("?format=json", {}),
        ("poll/?since_revision=invalid", {}),
        ("", {"HTTP_AUTHORIZATION": "Bearer invalid"}),
        ("", {"HTTP_COOKIE": f"{settings.SESSION_COOKIE_NAME}=invalid"}),
        ("", {"HTTP_ACCEPT": "text/html"}),
    ],
)
def test_non_public_cache_requests_use_full_api(
    client: Client, live_route: str, suffix: str, headers: dict[str, str]
) -> None:
    """Authentication, validation and content negotiation must never be skipped."""
    with patch("apps.schedule.api.public_live_cache.read_cached_live") as cached:
        client.get(live_route + suffix, **headers)
    cached.assert_not_called()


def test_cached_response_retains_host_https_and_cors_controls(
    client: Client, live_route: str
) -> None:
    """A warm entry cannot bypass outer Django security middleware."""
    with override_settings(ALLOWED_HOSTS=["testserver"]):
        assert (
            client.get(live_route, HTTP_HOST="untrusted.invalid").status_code
            == HTTPStatus.BAD_REQUEST
        )
    with override_settings(SECURE_SSL_REDIRECT=True):
        assert Client().get(live_route).status_code == HTTPStatus.MOVED_PERMANENTLY
    with override_settings(CORS_ALLOWED_ORIGINS=["https://spectator.example"]):
        response = client.get(live_route, HTTP_ORIGIN="https://spectator.example")
        assert response["Access-Control-Allow-Origin"] == "https://spectator.example"
