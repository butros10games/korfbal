"""Web push sending helpers.

Subscriptions that error with 404/410 are marked inactive.

Payload format is aligned with
`apps/node_projects/frontend/korfbal-web/public/sw-push.js`.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
from typing import Any

from apps.player.application.ports import WebPushClient, WebPushDeliveryError
from apps.player.models.push_subscription import PlayerPushSubscription


logger = logging.getLogger(__name__)


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


def send_to_model_subscription(
    *,
    sub: PlayerPushSubscription,
    payload: WebPushPayload,
    client: WebPushClient,
    ttl_seconds: int,
) -> None:
    """Send and mark dead subscriptions inactive."""
    try:
        client.send(
            subscription=sub.subscription,
            data=payload.to_json(),
            ttl_seconds=ttl_seconds,
        )
    except WebPushDeliveryError as exc:
        if exc.status_code in {404, 410}:
            logger.info(
                "Web push subscription expired; deactivating %s (status=%s)",
                sub.id_uuid,
                exc.status_code,
            )
            sub.is_active = False
            sub.save(update_fields=["is_active", "updated_at"])
            return

        logger.warning("Web push send failed for %s", sub.id_uuid, exc_info=True)
        raise
