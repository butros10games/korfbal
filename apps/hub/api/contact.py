"""Contact requests sent from the KorfConnect apps to the support inbox."""

from __future__ import annotations

import logging
from smtplib import SMTPException
from typing import Any

from django.conf import settings
from django.contrib.auth.models import AbstractBaseUser, AnonymousUser
from django.core.mail import EmailMessage
from django_ratelimit.core import is_ratelimited
from rest_framework import permissions, status
from rest_framework.request import Request
from rest_framework.response import Response

from apps.kwt_common.api.base import KorfbalAPIView

from .serializers import CONTACT_TOPIC_LABELS, ContactRequestSerializer


logger = logging.getLogger(__name__)
RequestUser = AbstractBaseUser | AnonymousUser

# Enough for a conversation, not enough for a spam run from one address.
CONTACT_REQUESTS_PER_ADDRESS = "5/h"
TOO_MANY_REQUESTS_DETAIL = (
    "Je hebt al een paar berichten gestuurd. Probeer het over een uur opnieuw."
)
SEND_FAILED_DETAIL = (
    "Versturen is mislukt. Probeer het later opnieuw of mail ons direct."
)


def _support_address() -> str:
    return str(
        getattr(settings, "KORFBAL_SUPPORT_EMAIL", "")
        or getattr(settings, "BG_AUTH_SUPPORT_EMAIL", "")
    )


def _account_email(user: RequestUser) -> str:
    """Return the signed-in account's address, or empty for visitors."""
    if not user.is_authenticated:
        return ""
    return str(getattr(user, "email", "") or "")


def _sender_line(name: str, email: str, user: RequestUser) -> str:
    who = name or (user.get_username() if user.is_authenticated else "") or "Onbekend"
    account = (
        f" (account: {user.get_username()})" if user.is_authenticated and name else ""
    )
    return f"{who}{account} <{email}>"


def build_contact_email(
    *,
    data: dict[str, Any],
    user: RequestUser,
) -> EmailMessage:
    """Assemble the plain-text support email for a validated contact request.

    Args:
        data: The validated serializer data.
        user: The requesting user (possibly anonymous).

    Returns:
        The message addressed to the support inbox with the sender as reply-to.

    """
    email = data.get("email") or _account_email(user)
    topic_label = CONTACT_TOPIC_LABELS[data["topic"]]
    context = data.get("context") or {}
    context_lines = [f"{key}: {value}" for key, value in context.items() if value] or [
        "(geen)"
    ]
    body = "\n".join(
        [
            f"Onderwerp: {topic_label}",
            f"Van: {_sender_line(data.get('name', ''), email, user)}",
            f"Ingelogd: {'ja' if user.is_authenticated else 'nee'}",
            "",
            data["message"],
            "",
            "Context:",
            *context_lines,
        ],
    )
    return EmailMessage(
        subject=f"[KorfConnect] {topic_label}",
        body=body,
        to=[_support_address()],
        reply_to=[email] if email else None,
    )


class ContactRequestView(KorfbalAPIView):
    """Accept a contact request and forward it to the support inbox."""

    permission_classes = (permissions.AllowAny,)
    serializer_class = ContactRequestSerializer

    def post(self, request: Request, *args: object, **kwargs: object) -> Response:
        """Validate the message, apply the per-address limit and send it."""
        serializer = ContactRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        user = request.user
        if not (data.get("email") or _account_email(user)):
            return Response(
                {"email": ["Vul een e-mailadres in zodat we kunnen antwoorden."]},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not _support_address():
            logger.error("Contact request dropped: no support address configured")
            return Response(
                {"detail": "Contact is tijdelijk niet beschikbaar."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        if is_ratelimited(
            request._request,
            group="korfbal.contact_request",
            key="ip",
            rate=CONTACT_REQUESTS_PER_ADDRESS,
            increment=True,
        ):
            return Response(
                {"detail": TOO_MANY_REQUESTS_DETAIL},
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )
        message = build_contact_email(data=data, user=user)
        try:
            message.send(fail_silently=False)
        except (SMTPException, OSError):
            logger.exception("Contact request could not be sent")
            return Response(
                {"detail": SEND_FAILED_DETAIL},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        return Response(
            {"detail": "Bericht verzonden."}, status=status.HTTP_202_ACCEPTED
        )
