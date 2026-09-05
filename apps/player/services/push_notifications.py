"""Push-notification helper logic for player API endpoints."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import logging
from typing import Any, Protocol

from django.conf import settings

from apps.player.application.ports import WebPushClient, WebPushDeliveryError
from apps.player.models.push_subscription import PlayerPushSubscription
from apps.player.services.web_push import (
    WebPushPayload,
    send_to_model_subscription,
)


logger = logging.getLogger(__name__)


class TestPushSender(Protocol):
    """Deliver a test payload to stored subscriptions."""

    def __call__(
        self,
        *,
        subs: list[PlayerPushSubscription],
        payload: WebPushPayload,
    ) -> tuple[int, int, list[dict[str, Any]]]:
        """Return sent, failed, and error details."""
        ...


@dataclass(slots=True)
class PushTestConfigurationError(Exception):
    """Raised when the test-push runtime is not configured."""

    detail: str
    missing: list[str]


class NoActivePushSubscriptionsError(Exception):
    """Raised when a user has no active push destinations."""


@dataclass(frozen=True, slots=True)
class PushTestResult:
    """Delivery totals returned by the test-push command."""

    total: int
    sent: int
    failed: int
    errors: list[dict[str, Any]]


def missing_webpush_settings() -> list[str]:
    """Return missing VAPID settings required for web push."""
    vapid_public = str(getattr(settings, "WEBPUSH_VAPID_PUBLIC_KEY", "") or "").strip()
    vapid_private = str(
        getattr(settings, "WEBPUSH_VAPID_PRIVATE_KEY", "") or ""
    ).strip()
    vapid_subject = str(getattr(settings, "WEBPUSH_VAPID_SUBJECT", "") or "").strip()

    return [
        name
        for name, value in [
            ("WEBPUSH_VAPID_PUBLIC_KEY", vapid_public),
            ("WEBPUSH_VAPID_PRIVATE_KEY", vapid_private),
            ("WEBPUSH_VAPID_SUBJECT", vapid_subject),
        ]
        if not value
    ]


def build_target_url() -> str:
    """Return the target URL used by the push test payload."""
    base_url = str(getattr(settings, "WEB_APP_ORIGIN", "") or "").rstrip("/")
    return f"{base_url}/profile" if base_url else "/profile"


def send_test_payload(
    *,
    subs: list[PlayerPushSubscription],
    payload: WebPushPayload,
    client: WebPushClient,
    ttl_seconds: int,
) -> tuple[int, int, list[dict[str, Any]]]:
    """Send a push payload to subscriptions and capture non-fatal errors."""
    sent = 0
    failed = 0
    errors: list[dict[str, Any]] = []

    for sub in subs:
        try:
            send_to_model_subscription(
                sub=sub,
                payload=payload,
                client=client,
                ttl_seconds=ttl_seconds,
            )
            sent += 1
        except WebPushDeliveryError as exc:
            failed += 1
            errors.append({
                "subscription_id": str(sub.id_uuid),
                "endpoint": str(sub.endpoint),
                "status_code": exc.status_code,
                "detail": str(exc),
            })
        except Exception as exc:
            failed += 1
            logger.warning(
                "Unexpected error while sending test web push to %s",
                sub.id_uuid,
                exc_info=True,
            )
            errors.append({
                "subscription_id": str(sub.id_uuid),
                "endpoint": str(sub.endpoint),
                "detail": str(exc) or "Unexpected error",
            })

    return sent, failed, errors


def send_test_push_notification(
    *,
    user_id: int,
    webpush_available: Callable[[], bool],
    send_pushes: TestPushSender,
) -> PushTestResult:
    """Send the standard test notification to a user's active subscriptions.

    Raises:
        NoActivePushSubscriptionsError: If the user has no active subscriptions.
        PushTestConfigurationError: If required settings or libraries are missing.

    """
    missing = missing_webpush_settings()
    if missing:
        raise PushTestConfigurationError(
            detail="Web push not configured",
            missing=missing,
        )
    if not webpush_available():
        raise PushTestConfigurationError(
            detail="Web push runtime is missing pywebpush",
            missing=["pywebpush"],
        )

    subscriptions = list(
        PlayerPushSubscription.objects.filter(
            user_id=user_id,
            is_active=True,
        ).order_by("-updated_at")
    )
    if not subscriptions:
        raise NoActivePushSubscriptionsError

    sent, failed, errors = send_pushes(
        subs=subscriptions,
        payload=WebPushPayload(
            title="Test pushmelding",
            body="Als je dit ziet werkt push via de PWA.",
            url=build_target_url(),
            tag="debug-test",
        ),
    )
    return PushTestResult(
        total=len(subscriptions),
        sent=sent,
        failed=failed,
        errors=errors,
    )
