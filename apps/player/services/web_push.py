"""Web push sending helpers.

This module is intentionally small and defensive:
- If VAPID settings are missing, sends are skipped (no hard crash).
- Subscriptions that error with 404/410 are marked inactive.

Payload format is aligned with
`apps/node_projects/frontend/korfbal-web/public/sw-push.js`.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
from typing import Any, Protocol

from django.conf import settings

from apps.player.models.push_subscription import PlayerPushSubscription


logger = logging.getLogger(__name__)


WebPushException: type[Exception]
webpush: Any
try:
    from pywebpush import WebPushException as _ImportedWebPushException
    from pywebpush import webpush as _imported_webpush
except Exception:  # pragma: no cover
    # `pywebpush` is a runtime dependency; keep imports safe for tooling.
    WebPushException = Exception
    webpush = None
else:
    WebPushException = _ImportedWebPushException
    webpush = _imported_webpush


class WebPushClient(Protocol):
    """Outbound web-push provider port."""

    def send(
        self,
        *,
        subscription: dict[str, Any],
        data: str,
        ttl_seconds: int,
    ) -> None:
        """Send a web-push payload."""


class PyWebPushClient:
    """Production web-push client backed by pywebpush."""

    def send(
        self,
        *,
        subscription: dict[str, Any],
        data: str,
        ttl_seconds: int,
    ) -> None:
        """Send a web-push payload with pywebpush."""
        if webpush is None:
            raise RuntimeError(
                "pywebpush is not available in this runtime; cannot send web push"
            )

        webpush(
            subscription_info=subscription,
            data=data,
            vapid_private_key=str(settings.WEBPUSH_VAPID_PRIVATE_KEY),
            vapid_claims=_vapid_claims(),
            ttl=ttl_seconds,
        )


DEFAULT_WEB_PUSH_CLIENT = PyWebPushClient()


@dataclass(frozen=True, slots=True)
class WebPushPayload:
    """Payload sent to the PWA service worker."""

    title: str
    body: str
    url: str
    tag: str | None = None
    icon: str | None = None
    badge: str | None = None
    data: dict[str, Any] | None = None

    def to_json(self) -> str:
        """Serialise payload to JSON for the web-push provider."""
        payload: dict[str, Any] = {
            "title": self.title,
            "body": self.body,
            "url": self.url,
        }
        if self.tag:
            payload["tag"] = self.tag
        if self.icon:
            payload["icon"] = self.icon
        if self.badge:
            payload["badge"] = self.badge
        if self.data:
            payload["data"] = self.data
        return json.dumps(payload)


def _webpush_configured() -> bool:
    return bool(
        getattr(settings, "WEBPUSH_VAPID_PUBLIC_KEY", "")
        and getattr(settings, "WEBPUSH_VAPID_PRIVATE_KEY", "")
        and getattr(settings, "WEBPUSH_VAPID_SUBJECT", "")
    )


def _vapid_claims() -> dict[str, str | int]:
    return {"sub": str(getattr(settings, "WEBPUSH_VAPID_SUBJECT", ""))}


def webpush_library_available() -> bool:
    """Return whether the runtime has the web-push library installed."""
    return webpush is not None


def send_to_subscription(
    *,
    subscription: dict[str, Any],
    payload: WebPushPayload,
    ttl: int | None = None,
    client: WebPushClient | None = None,
) -> None:
    """Send a single web push message.

    Notes:
        This function is defensive: if VAPID settings are missing, it will log and
        return without sending.

    Raises:
        RuntimeError: When the `pywebpush` library is not available in the runtime.

    """
    if not _webpush_configured():
        logger.info("Web push not configured; skipping send")
        return

    ttl_seconds = int(ttl or getattr(settings, "WEBPUSH_TTL_SECONDS", 3600) or 3600)

    (client or DEFAULT_WEB_PUSH_CLIENT).send(
        subscription=subscription,
        data=payload.to_json(),
        ttl_seconds=ttl_seconds,
    )


def send_to_model_subscription(
    *,
    sub: PlayerPushSubscription,
    payload: WebPushPayload,
    client: WebPushClient | None = None,
) -> None:
    """Send and mark dead subscriptions inactive."""
    try:
        send_to_subscription(
            subscription=sub.subscription,
            payload=payload,
            client=client,
        )
    except WebPushException as exc:  # pragma: no cover
        status_code = getattr(getattr(exc, "response", None), "status_code", None)
        if status_code in {404, 410}:
            logger.info(
                "Web push subscription expired; deactivating %s (status=%s)",
                sub.id_uuid,
                status_code,
            )
            sub.is_active = False
            sub.save(update_fields=["is_active", "updated_at"])
            return

        logger.warning("Web push send failed for %s", sub.id_uuid, exc_info=True)
        raise
