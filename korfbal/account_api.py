"""JSON account adapters for routes whose shared counterparts render HTML."""

from __future__ import annotations

from collections.abc import Callable
from http import HTTPStatus
from typing import cast

from bg_auth.services.account import AccountService
from bg_auth.utils import send_confirmation_code
from bg_auth.views.api.utils import json_response
from django.contrib.auth import logout
from django.contrib.auth.models import User
from django.contrib.auth.tokens import default_token_generator
from django.core import signing
from django.http import HttpRequest, HttpResponseBase
from django.utils.http import urlsafe_base64_decode
from django.views.decorators.http import require_GET, require_POST
from django_ratelimit.decorators import ratelimit


View = Callable[..., HttpResponseBase]
require_post = cast(Callable[[View], View], require_POST)
require_get = cast(Callable[[View], View], require_GET)


@require_post
def logout_account(request: HttpRequest) -> HttpResponseBase:
    """End a browser session without redirecting to an absent HTML login route."""
    if not request.user.is_authenticated:
        return json_response(
            {"detail": "Authentication required."}, status=HTTPStatus.UNAUTHORIZED
        )
    logout(request)
    return json_response({"status": "ok"})


@require_get
def activate_account(request: HttpRequest, uidb64: str, token: str) -> HttpResponseBase:
    """Activate a valid emailed link and return a readable API result."""
    user_model = User
    try:
        uid = urlsafe_base64_decode(uidb64).decode("utf-8")
        user = user_model.objects.get(pk=uid)
    except (TypeError, ValueError, OverflowError, user_model.DoesNotExist):
        user = None
    if user is None or not default_token_generator.check_token(user, token):
        return json_response(
            {"detail": "Activation link is invalid or has expired."},
            status=HTTPStatus.BAD_REQUEST,
        )
    user.is_active = True
    user.save(update_fields=["is_active"])
    return json_response({
        "status": "ok",
        "message": "Your account has been activated. You can now log in.",
    })


@require_post
@ratelimit(key="ip", rate="2/m", method="POST", block=False)
def resend_confirmation(request: HttpRequest, token: str) -> HttpResponseBase:
    """Resend a confirmation without revealing whether an account exists."""
    if getattr(request, "limited", False):
        response = json_response(
            {"detail": "Too many requests. Please wait a minute and try again."},
            status=HTTPStatus.TOO_MANY_REQUESTS,
        )
        response["Retry-After"] = "60"
        return response
    try:
        email = AccountService.resolve_resend_token(token)
    except signing.BadSignature:
        email = None
    if email:
        users = list(User.objects.filter(email__iexact=email)[:2])
        if (
            len(users) == 1
            and not users[0].is_active
            and not send_confirmation_code(request, users[0])
        ):
            return json_response(
                {"detail": "Activation email is unavailable. Try again later."},
                status=HTTPStatus.SERVICE_UNAVAILABLE,
            )
    return json_response({
        "status": "ok",
        "message": "If an inactive account exists, an activation link has been sent.",
    })
