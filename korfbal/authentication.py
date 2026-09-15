"""JWT Bearer authentication for Django REST Framework using BG Auth."""

from __future__ import annotations

from typing import Any

from bg_auth.jwt import (
    JwtError,
    credentials_are_current,
    decode as decode_jwt,
)
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AbstractBaseUser
from django.core.exceptions import ValidationError
from drf_spectacular.extensions import OpenApiAuthenticationExtension
from rest_framework.authentication import BaseAuthentication, get_authorization_header
from rest_framework.exceptions import AuthenticationFailed


class JwtBearerAuthentication(BaseAuthentication):
    """Authenticate requests using BG Auth JWT bearer tokens."""

    keyword = "bearer"

    def _extract_bearer_token(self, auth_header: str) -> str | None:
        """Extract a bare token string from an Authorization header.

        Returns None for other authentication schemes. An explicitly empty
        bearer credential is an authentication failure.

        Raises:
            AuthenticationFailed: If the bearer credential is empty.

        """
        if not auth_header:
            return None

        parts = auth_header.split(maxsplit=1)
        if not parts:
            return None
        scheme = parts[0]
        token = parts[1] if len(parts) > 1 else ""

        if scheme.lower() != self.keyword:
            return None

        token = token.strip().strip('"').strip("'")
        if token.lower().startswith("bearer "):
            token = token.split(" ", 1)[1].strip()

        if not token:
            raise AuthenticationFailed("Invalid access token")
        return token

    def authenticate(self, request: Any) -> tuple[AbstractBaseUser, str] | None:
        """Authenticate the request using a JWT bearer token.

        This method delegates header parsing to `_extract_bearer_token` to keep
        complexity low.

        Raises:
            AuthenticationFailed: If the provided token is invalid, the user is
                not found, inactive, or otherwise invalid.

        """
        try:
            auth_header = get_authorization_header(request).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise AuthenticationFailed("Invalid access token") from exc

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
        except (ValidationError, ValueError, TypeError, OverflowError) as exc:
            raise AuthenticationFailed("Invalid access token") from exc
        except user_model.DoesNotExist as exc:
            raise AuthenticationFailed("User not found") from exc

        if not getattr(user, "is_active", False):
            raise AuthenticationFailed("User is inactive")

        if not isinstance(user, AbstractBaseUser):
            raise AuthenticationFailed("Invalid user")

        if not credentials_are_current(payload, user):
            raise AuthenticationFailed("Invalid access token")

        return user, token

    def authenticate_header(self, request: Any) -> str:
        """Return the challenge used when bearer authentication fails."""
        del request
        return "Bearer"


class JwtBearerAuthenticationScheme(OpenApiAuthenticationExtension):
    """Describe the custom BG Auth bearer token in the OpenAPI contract."""

    target_class = JwtBearerAuthentication
    name = "jwtBearerAuth"

    def get_security_definition(self, auto_schema: object) -> dict[str, str]:
        """Return the OpenAPI HTTP bearer security scheme."""
        del auto_schema
        return {
            "type": "http",
            "scheme": "bearer",
            "bearerFormat": "JWT",
        }
