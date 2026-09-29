"""The APNs adapter signs provider tokens and classifies rejections."""

from __future__ import annotations

from collections.abc import Callable
from http import HTTPStatus
import json

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
import httpx
import jwt
import pytest

from apps.player.adapters.outbound.apns import ApnsLiveActivityClient
from apps.player.application.ports import LiveActivityDeliveryError


def private_key_pem() -> str:
    """Return a fresh P-256 key in the PKCS8 PEM form Apple issues."""
    key = ec.generate_private_key(ec.SECP256R1())
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


def build_client(
    handler: Callable[[httpx.Request], httpx.Response],
) -> ApnsLiveActivityClient:
    """Build a configured client whose HTTP goes to ``handler``."""
    return ApnsLiveActivityClient(
        team_id="TEAM123456",
        key_id="KEY1234567",
        private_key=private_key_pem(),
        bundle_id="korfbal.butrosgroot.com",
        transport=httpx.MockTransport(handler),
    )


def test_send_uses_live_activity_headers_and_signed_provider_token() -> None:
    """Requests carry the liveactivity push type, topic suffix and ES256 JWT."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200)

    client = build_client(handler)
    client.send(token="ab" * 16, payload={"aps": {"event": "update"}})
    client.send(token="ab" * 16, payload={"aps": {"event": "update"}})
    request = seen[0]
    assert request.url == httpx.URL("https://api.push.apple.com/3/device/" + "ab" * 16)
    assert request.headers["apns-push-type"] == "liveactivity"
    assert request.headers["apns-topic"] == (
        "korfbal.butrosgroot.com.push-type.liveactivity"
    )
    assert request.headers["apns-priority"] == "10"
    assert json.loads(request.content) == {"aps": {"event": "update"}}
    bearer = request.headers["authorization"].removeprefix("bearer ")
    header = jwt.get_unverified_header(bearer)
    assert (header["alg"], header["kid"]) == ("ES256", "KEY1234567")
    assert jwt.decode(bearer, options={"verify_signature": False})["iss"] == (
        "TEAM123456"
    )
    # The provider token is reused between pushes instead of re-signed.
    assert seen[1].headers["authorization"] == request.headers["authorization"]


def test_rejections_are_classified_and_expired_provider_tokens_refresh() -> None:
    """Dead device tokens are permanent; an expired JWT is re-signed next time."""
    responses = iter([
        httpx.Response(410, json={"reason": "Unregistered"}),
        httpx.Response(403, json={"reason": "ExpiredProviderToken"}),
        httpx.Response(200),
    ])
    bearers: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bearers.append(request.headers["authorization"])
        return next(responses)

    client = build_client(handler)
    with pytest.raises(LiveActivityDeliveryError) as dead:
        client.send(token="cd" * 16, payload={})
    assert dead.value.permanent
    assert dead.value.status_code == HTTPStatus.GONE
    with pytest.raises(LiveActivityDeliveryError) as expired:
        client.send(token="cd" * 16, payload={})
    assert not expired.value.permanent
    assert expired.value.reason == "ExpiredProviderToken"
    client.send(token="cd" * 16, payload={})
    assert bearers[0] == bearers[1] != bearers[2]


def test_unconfigured_client_reports_missing_credentials() -> None:
    """A missing key never reaches the network and is not a dead token."""
    client = ApnsLiveActivityClient(
        team_id="", key_id="", private_key="", bundle_id="korfbal.butrosgroot.com"
    )
    assert not client.configured
    with pytest.raises(LiveActivityDeliveryError) as error:
        client.send(token="ef" * 16, payload={})
    assert error.value.reason == "NotConfigured"
    assert not error.value.permanent


def test_sandbox_environment_targets_the_sandbox_host() -> None:
    """Development builds use Apple's sandbox gateway."""
    client = ApnsLiveActivityClient(
        team_id="T", key_id="K", private_key="x", bundle_id="b", sandbox=True
    )
    assert client.base_url == "https://api.sandbox.push.apple.com"
