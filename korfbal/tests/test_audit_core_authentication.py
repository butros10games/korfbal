"""Regression tests for the project's DRF JWT authentication adapter."""

from __future__ import annotations

from collections.abc import Callable
from http import HTTPStatus
from typing import cast

from bg_auth.jwt import issue_access_token, issue_pair
from django.contrib.auth.models import User
from django.http import HttpRequest
import pytest
from rest_framework.decorators import (
    api_view,
    authentication_classes,
    permission_classes,
)
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.test import APIRequestFactory

from korfbal.authentication import (
    JwtBearerAuthentication,
    JwtBearerAuthenticationScheme,
)


def _request(authorization: str | None = None) -> HttpRequest:
    if authorization is None:
        return APIRequestFactory().get("/")
    return APIRequestFactory().get("/", HTTP_AUTHORIZATION=authorization)


@api_view(["GET"])
@authentication_classes([JwtBearerAuthentication])
@permission_classes([IsAuthenticated])
def _protected_probe(_request: Request) -> Response:
    return Response({"ok": True})


@pytest.mark.parametrize(
    "authorization",
    [None, "", "Basic credentials", "Bearer", "Bearer   "],
)
def test_non_bearer_or_empty_credentials_do_not_claim_the_request(
    authorization: str | None,
) -> None:
    """Other authenticators may handle requests without a usable bearer token."""
    assert JwtBearerAuthentication().authenticate(_request(authorization)) is None


def test_non_utf8_authorization_header_returns_json_unauthorized() -> None:
    """Malformed wire bytes must not escape DRF as an unhandled server error."""
    request = APIRequestFactory().get("/", HTTP_AUTHORIZATION=b"Bearer \xff")

    view = cast(Callable[[HttpRequest], Response], _protected_probe)
    response = view(request)
    response.render()

    assert response.status_code == HTTPStatus.UNAUTHORIZED
    assert response.headers["Content-Type"] == "application/json"
    assert response.data == {"detail": "Invalid access token"}


@pytest.mark.django_db
def test_access_token_authenticates_its_active_user() -> None:
    """The project adapter and shared token issuer must interoperate."""
    user = User.objects.create_user(username="jwt-active-user")
    token, _expires_at = issue_access_token(user)

    authenticated_user, authenticated_token = JwtBearerAuthentication().authenticate(
        _request(f"Bearer {token}"),
    ) or (None, None)

    assert authenticated_user == user
    assert authenticated_token == token


@pytest.mark.django_db
def test_refresh_token_cannot_authenticate_an_api_request() -> None:
    """Only access tokens may be used as API bearer credentials."""
    user = User.objects.create_user(username="jwt-refresh-user")
    token = issue_pair(user).refresh

    with pytest.raises(AuthenticationFailed, match="Invalid access token"):
        JwtBearerAuthentication().authenticate(_request(f"Bearer {token}"))


@pytest.mark.django_db
def test_token_for_deleted_user_is_rejected_without_leaking_details() -> None:
    """A signed token must not outlive the local account it represents."""
    user = User.objects.create_user(username="jwt-deleted-user")
    token, _expires_at = issue_access_token(user)
    user.delete()

    with pytest.raises(AuthenticationFailed, match="User not found"):
        JwtBearerAuthentication().authenticate(_request(f"Bearer {token}"))


@pytest.mark.django_db
def test_token_for_inactive_user_is_rejected() -> None:
    """Disabling an account must immediately revoke its API access."""
    user = User.objects.create_user(username="jwt-inactive-user")
    token, _expires_at = issue_access_token(user)
    user.is_active = False
    user.save(update_fields=["is_active"])

    with pytest.raises(AuthenticationFailed, match="User is inactive"):
        JwtBearerAuthentication().authenticate(_request(f"Bearer {token}"))


def test_openapi_scheme_identifies_jwt_bearer_authentication() -> None:
    """Generated clients must receive the correct authentication contract."""
    assert JwtBearerAuthenticationScheme(None).get_security_definition(None) == {
        "type": "http",
        "scheme": "bearer",
        "bearerFormat": "JWT",
    }
