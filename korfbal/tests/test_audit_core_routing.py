"""Regression tests for project-level HTTP and ASGI routing boundaries."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from django.urls import Resolver404, resolve, reverse
import pytest

from korfbal import asgi


@pytest.mark.parametrize(
    ("route", "url_name"),
    [
        ("/auth/session/", "auth-session"),
        ("/auth/login/", "auth-login"),
        ("/auth/logout/", "auth-logout"),
        ("/auth/password-reset/request/", "auth-password-reset-request"),
        ("/api/auth/session/", "auth-session"),
        ("/api/auth/login/", "auth-login"),
        ("/api/auth/logout/", "auth-logout"),
        ("/api/auth/password-reset/request/", "auth-password-reset-request"),
        ("/auth/register/", "auth-register"),
        ("/api/auth/register/", "auth-register"),
        ("/auth/jwt/login/", "auth-jwt-login"),
        ("/api/auth/jwt/login/", "auth-jwt-login"),
        ("/auth/jwt/refresh/", "auth-jwt-refresh"),
        ("/api/auth/jwt/refresh/", "auth-jwt-refresh"),
        (
            "/auth/passkeys/authentication/options/",
            "auth-passkey-authentication-options",
        ),
        (
            "/api/auth/passkeys/registration/options/",
            "auth-passkey-registration-options",
        ),
    ],
)
def test_spa_auth_endpoints_have_primary_and_legacy_routes(
    route: str,
    url_name: str,
) -> None:
    """Session clients retain both API host and legacy-prefixed contracts."""
    assert resolve(route).url_name == url_name


@pytest.mark.parametrize(
    "route",
    [
        "/login/",
        "/register/",
        "/password-reset/",
    ],
)
def test_retired_server_rendered_auth_pages_are_not_routable(route: str) -> None:
    """The API host must not accidentally re-expose the old HTML auth surface."""
    with pytest.raises(Resolver404):
        resolve(route)


@pytest.mark.asyncio
async def test_asgi_routes_only_the_exact_http_stream_path_to_sse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The SSE bypass is intentionally limited to one HTTP endpoint."""
    destinations: list[str] = []

    async def record_sse(*_args: object) -> None:
        destinations.append("sse")
        await asyncio.sleep(0)

    async def record_django(*_args: object) -> None:
        destinations.append("django")
        await asyncio.sleep(0)

    monkeypatch.setattr(asgi, "sse_application", record_sse)
    monkeypatch.setattr(asgi, "django_application", record_django)

    receive: Callable[[], Awaitable[dict[str, object]]]
    send: Callable[[dict[str, object]], Awaitable[None]]

    async def receive() -> dict[str, object]:
        await asyncio.sleep(0)
        return {}

    async def send(_message: dict[str, object]) -> None:
        await asyncio.sleep(0)

    scopes: list[tuple[dict[str, object], str]] = [
        ({"type": "http", "path": "/api/live/events/"}, "sse"),
        ({"type": "http", "path": "/api/live/events"}, "django"),
        ({"type": "websocket", "path": "/api/live/events/"}, "django"),
        ({"type": "http", "path": "/api/matches/"}, "django"),
    ]
    for scope, expected_destination in scopes:
        destinations.clear()
        await asgi.application(scope, receive, send)
        assert destinations == [expected_destination]


@pytest.mark.parametrize(
    ("url_name", "kwargs"),
    [
        ("media-download", {}),
        ("auth-session", {}),
        (
            "club-logo",
            {"club_id": "00000000-0000-4000-8000-000000000001", "version": "a" * 64},
        ),
        ("player-song-clip", {"song_id": "00000000-0000-4000-8000-000000000001"}),
    ],
)
def test_generated_api_links_use_the_canonical_prefix(
    url_name: str, kwargs: dict[str, str]
) -> None:
    """The production edge forwards only `/api/*`; generated links must use it."""
    assert reverse(url_name, kwargs=kwargs).startswith("/api/")


SYNTHETIC_ID = "00000000-0000-4000-8000-000000000001"


@pytest.mark.parametrize("prefix", ["/api/match/", "/match/", "/match/api/"])
def test_lineup_reads_share_one_view_on_every_mount(prefix: str) -> None:
    """Mounts differ only by prefix, so authorization cannot diverge between them."""
    path = f"players_team/{SYNTHETIC_ID}/{SYNTHETIC_ID}/"
    assert resolve(prefix + path).func is resolve(f"/api/match/{path}").func
