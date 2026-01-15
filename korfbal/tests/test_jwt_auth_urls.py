"""URL resolution tests for JWT auth endpoints."""

from __future__ import annotations

from django.urls import resolve


def test_jwt_login_url_is_exposed() -> None:
    """JWT login endpoint should be routable (root + /api/ back-compat)."""
    assert resolve("/auth/jwt/login/").url_name == "auth-jwt-login"
    # Backwards-compat prefix is included in `korfbal.urls`
    assert resolve("/api/auth/jwt/login/").url_name == "auth-jwt-login"


def test_jwt_refresh_url_is_exposed() -> None:
    """JWT refresh endpoint should be routable (root + /api/ back-compat)."""
    assert resolve("/auth/jwt/refresh/").url_name == "auth-jwt-refresh"
    assert resolve("/api/auth/jwt/refresh/").url_name == "auth-jwt-refresh"
