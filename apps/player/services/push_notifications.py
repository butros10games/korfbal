"""Push-notification helper logic for player API endpoints."""

from __future__ import annotations

import logging
from typing import Any

from django.conf import settings

from apps.player.application.ports import WebPushClient, WebPushDeliveryError
from apps.player.models.push_subscription import PlayerPushSubscription
from apps.player.services.web_push import (
    WebPushPayload,
    send_to_model_subscription,
)


logger = logging.getLogger(__name__)


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
