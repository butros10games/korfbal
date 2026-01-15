"""API-only URL configuration for authentication.

This project used to include `bg_auth.urls` at the root, which also exposes
server-rendered pages (/login, /register, password reset HTML pages, etc).

Now that Korfbal uses a React SPA frontend, this module re-exports only the
JSON API endpoints needed by the SPA (session/login/logout/password reset/etc)
so we can remove the old Django frontend routes from `korfbal.urls`.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import cast

from bg_auth import views
from django.http import HttpResponseBase
from django.urls import path


ViewType = Callable[..., HttpResponseBase]


urlpatterns = [
    # Session + login
    path(
        "auth/session/",
        cast(ViewType, views.api.session_status),
        name="auth-session",
    ),
    path(
        "auth/login/",
        cast(ViewType, views.api.login_user_api),
        name="auth-login",
    ),
    path(
        "auth/login/2fa/verify/",
        cast(ViewType, views.api.verify_two_factor_code),
        name="auth-login-2fa-verify",
    ),
    path(
        "auth/login/2fa/resend/",
        cast(ViewType, views.api.resend_two_factor_code),
        name="auth-login-2fa-resend",
    ),
    # JWT auth (mobile-friendly)
    # These map to `bg_auth.views.api.jwt_*` and are exposed without an extra
    # /api prefix because this project serves the API on a dedicated domain.
    path(
        "auth/jwt/login/",
        cast(ViewType, views.api.jwt_login),
        name="auth-jwt-login",
    ),
    path(
        "auth/jwt/login/2fa/verify/",
        cast(ViewType, views.api.jwt_verify_two_factor),
        name="auth-jwt-login-2fa-verify",
    ),
    path(
        "auth/jwt/refresh/",
        cast(ViewType, views.api.jwt_refresh),
        name="auth-jwt-refresh",
    ),
    # Password reset (API-driven; SPA renders the UI)
    path(
        "auth/password-reset/request/",
        cast(ViewType, views.api.password_reset_request_api),
        name="auth-password-reset-request",
    ),
    path(
        "auth/password-reset/confirm/<uidb64>/<token>/",
        cast(ViewType, views.api.password_reset_confirm_api),
        name="auth-password-reset-confirm",
    ),
    # Logout
    path("auth/logout/", views.api.logout_user, name="auth-logout"),
    # Back-compat: some clients hit /api/logout
    path("logout/", views.api.logout_user, name="logout"),
    path("logout", views.api.logout_user),  # prevent 301 in tests
    # Account activation + email confirmation
    path(
        "activate/<uidb64>/<token>/",
        views.api.activate_account,
        name="activate",
    ),
    path(
        "resend-confirmation/<str:token>/",
        cast(ViewType, views.api.resend_confirmation_email),
        name="resend_confirmation",
    ),
]
