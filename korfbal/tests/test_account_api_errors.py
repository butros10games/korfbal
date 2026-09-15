"""Account routes must keep failures and successful responses in the API contract."""

from http import HTTPStatus
from unittest.mock import Mock

from bg_auth.services.account import AccountService
from django.contrib.auth.models import User
from django.contrib.auth.tokens import default_token_generator
from django.core.cache import cache
from django.test import Client
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode
import pytest


pytestmark = pytest.mark.django_db


@pytest.mark.parametrize(
    "url", ["/auth/logout/", "/api/auth/logout/", "/logout", "/api/logout"]
)
def test_logout_is_json_without_accept_header_and_get_does_not_log_out(
    client: Client, url: str
) -> None:
    """Logout must neither redirect to a removed login route nor mutate on GET."""
    user = User.objects.create_user(username="logout-adapter")
    client.force_login(user)
    assert client.get(url).status_code == HTTPStatus.METHOD_NOT_ALLOWED
    assert client.get("/auth/session/").json()["authenticated"] is True
    response = client.post(url)
    assert response.status_code == HTTPStatus.OK
    assert response.json() == {"status": "ok"}
    assert client.get("/auth/session/").json()["authenticated"] is False
    assert client.post(url).status_code == HTTPStatus.UNAUTHORIZED


@pytest.mark.parametrize("valid", [True, False])
def test_activation_returns_json_and_only_activates_valid_link(
    client: Client, valid: bool
) -> None:
    """A bad token returns 400; a valid email link activates its account with 200."""
    user = User.objects.create_user(username="activation-adapter", is_active=False)
    uid = urlsafe_base64_encode(force_bytes(user.pk))
    token = default_token_generator.make_token(user) if valid else "invalid"
    response = client.get(f"/activate/{uid}/{token}/")
    assert response.status_code == (HTTPStatus.OK if valid else HTTPStatus.BAD_REQUEST)
    assert response.json()["message"]
    user.refresh_from_db()
    assert user.is_active is valid


def test_invalid_activation_user_returns_bad_request(client: Client) -> None:
    """Malformed activation identifiers must not produce decoding/redirect errors."""
    response = client.get("/activate/not-a-user/invalid/")
    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert response.json()["detail"] == "Activation link is invalid or has expired."


@pytest.mark.parametrize("available", [True, False])
def test_resend_confirmation_reports_delivery_availability(
    client: Client, monkeypatch: pytest.MonkeyPatch, available: bool
) -> None:
    """Unavailable delivery is 503 and neither path tries to reverse HTML login."""
    user = User.objects.create_user(
        username="resend-adapter", email="synthetic@example.test", is_active=False
    )
    send = Mock(return_value=available)
    monkeypatch.setattr("korfbal.account_api.send_confirmation_code", send)
    token = AccountService.build_resend_token(user.email)
    response = client.post(f"/resend-confirmation/{token}/")
    assert response.status_code == (
        HTTPStatus.OK if available else HTTPStatus.SERVICE_UNAVAILABLE
    )
    assert response.json()["message"]
    send.assert_called_once()


def test_resend_rate_limit_returns_429_with_retry_hint(client: Client) -> None:
    """The limiter must reach the API's 429 response rather than Django's 403."""
    cache.clear()
    responses = [client.post("/resend-confirmation/invalid/") for _ in range(3)]
    assert [response.status_code for response in responses] == [200, 200, 429]
    assert responses[-1]["Retry-After"] == "60"
    assert responses[-1].json()["code"] == "throttled"
