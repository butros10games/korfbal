"""JWT Bearer authentication for Django REST Framework using BG Auth."""

from __future__ import annotations

from typing import Any

from bg_auth.jwt import (
    JwtError,
    decode as decode_jwt,
)
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AbstractBaseUser
from rest_framework.authentication import BaseAuthentication, get_authorization_header
from rest_framework.exceptions import AuthenticationFailed


class JwtBearerAuthentication(BaseAuthentication):
    """Authenticate requests using BG Auth JWT bearer tokens."""

    keyword = "bearer"

    def _extract_bearer_token(self, auth_header: str) -> str | None:
        """Extract a bare token string from an Authorization header.

        Returns the token string or None if the header is malformed / not a
        bearer token.
        """
        if not auth_header:
            return None

        try:
            scheme, token = auth_header.split(" ", 1)
        except ValueError:
            return None

        if scheme.lower() != self.keyword:
            return None

        token = token.strip().strip('"').strip("'")
        if token.lower().startswith("bearer "):
            token = token.split(" ", 1)[1].strip()

        return token or None

    def authenticate(self, request: Any) -> tuple[AbstractBaseUser, str] | None:  # noqa: ANN401 - DRF interface
        """Authenticate the request using a JWT bearer token.

        This method delegates header parsing to `_extract_bearer_token` to keep
        complexity low.

        Raises:
            AuthenticationFailed: If the provided token is invalid, the user is
                not found, inactive, or otherwise invalid.

        """
        auth_header = get_authorization_header(request).decode("utf-8")
        token = self._extract_bearer_token(auth_header)
        if not token:
            return None

        try:
            payload = decode_jwt(token, expected_type="access")
        except JwtError as exc:
            raise AuthenticationFailed("Invalid access token") from exc

        user_id = payload.get("sub")
        if not user_id:
            raise AuthenticationFailed("Invalid access token")

        user_model = get_user_model()
        try:
            user = user_model.objects.get(pk=user_id)
        except user_model.DoesNotExist as exc:
            raise AuthenticationFailed("User not found") from exc

        if not getattr(user, "is_active", False):
            raise AuthenticationFailed("User is inactive")

        if not isinstance(user, AbstractBaseUser):
            raise AuthenticationFailed("Invalid user")

        return user, token
