"""Client-address resolution behind the reverse proxy."""

from __future__ import annotations

from http import HTTPStatus
import json

from django.test import Client, override_settings
import pytest

from apps.kwt_common.middleware.trusted_proxy import parse_networks, resolve_client_ip


PRIVATE_PROXIES = ("10.0.0.0/8", "172.16.0.0/12")
LOGIN_LIMIT_PER_MINUTE = 5


def _login(client: Client, real_ip: str) -> int:
    return client.post(
        "/api/auth/jwt/login/",
        data=json.dumps({"username": "nobody", "password": "wrong"}),
        content_type="application/json",
        headers={"X-Real-IP": real_ip},
    ).status_code


@pytest.mark.parametrize(
    ("meta", "expected"),
    [
        ({"REMOTE_ADDR": "10.0.0.2", "HTTP_X_REAL_IP": "198.51.100.7"}, "198.51.100.7"),
        (
            {
                "REMOTE_ADDR": "10.0.0.2",
                "HTTP_X_FORWARDED_FOR": "1.1.1.1, 198.51.100.8, 172.18.0.3",
            },
            "198.51.100.8",
        ),
        ({"REMOTE_ADDR": "10.0.0.2", "HTTP_X_REAL_IP": "not-an-ip"}, "10.0.0.2"),
    ],
)
def test_resolve_client_ip_prefers_proxy_supplied_address(
    meta: dict[str, str], expected: str
) -> None:
    """Only the hops our own proxies appended are trusted."""
    assert resolve_client_ip(meta, parse_networks(PRIVATE_PROXIES)) == expected


@pytest.mark.django_db
@override_settings(KORFBAL_TRUSTED_PROXIES=["127.0.0.0/8"])
def test_login_rate_limit_is_per_client_behind_the_proxy() -> None:
    """One noisy client must not exhaust the limit for everyone else."""
    client = Client()
    statuses = [
        _login(client, "198.51.100.10") for _ in range(LOGIN_LIMIT_PER_MINUTE + 1)
    ]

    assert statuses[-1] == HTTPStatus.TOO_MANY_REQUESTS
    assert _login(client, "198.51.100.11") == HTTPStatus.BAD_REQUEST


@pytest.mark.django_db
@override_settings(KORFBAL_TRUSTED_PROXIES=["10.0.0.0/8"])
def test_untrusted_peers_cannot_spoof_forwarded_headers() -> None:
    """A direct caller cannot rotate its address or claim HTTPS."""
    client = Client(REMOTE_ADDR="203.0.113.50")
    statuses = [
        _login(client, f"198.51.100.{index}")
        for index in range(LOGIN_LIMIT_PER_MINUTE + 1)
    ]

    assert statuses[-1] == HTTPStatus.TOO_MANY_REQUESTS
    response = client.get(
        "/api/auth/session/", HTTP_X_FORWARDED_PROTO="https", secure=False
    )
    assert response.wsgi_request.META.get("HTTP_X_FORWARDED_PROTO") is None
    assert not response.wsgi_request.is_secure()
