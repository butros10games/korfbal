"""Require a completed application MFA flow for every administrative session."""

from collections.abc import Callable

from django.conf import settings
from django.contrib.auth import logout
from django.http import (
    HttpRequest,
    HttpResponse,
    HttpResponseForbidden,
    HttpResponseRedirect,
)
from django.utils.crypto import constant_time_compare


class AdminMfaMiddleware:
    """Keep the stock admin password form from creating an alternate login path."""

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        """Retain the next handler."""
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        """Require a password-bound MFA marker and disable the stock login form."""
        if request.path == "/admin" or request.path.startswith("/admin/"):
            user = request.user
            if user.is_authenticated and not getattr(user, "is_staff", False):
                return HttpResponseForbidden(
                    "Administrative access requires staff privileges."
                )
            verified = request.session.get("bg_auth_mfa_verified", "")
            if not (
                user.is_authenticated
                and user.is_active
                and getattr(user, "is_staff", False)
                and isinstance(verified, str)
                and constant_time_compare(verified, user.get_session_auth_hash())
            ):
                if user.is_authenticated:
                    logout(request)
                return HttpResponseRedirect(f"{settings.WEB_APP_ORIGIN}/sign-in")
            if request.path.rstrip("/") == "/admin/login":
                return HttpResponseRedirect("/admin/")
        return self.get_response(request)
