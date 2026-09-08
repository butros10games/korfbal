"""Bounded browser-push concurrency with database writes kept on the caller thread."""

from concurrent.futures import ThreadPoolExecutor
from itertools import batched
import logging

from django.utils import timezone

from apps.player.application.ports import WebPushClient, WebPushDeliveryError
from apps.player.models.push_subscription import PlayerPushSubscription
from apps.player.services.web_push import WebPushPayload


logger = logging.getLogger(__name__)
WEB_PUSH_WORKERS = 4


def send_web_push_batch(
    *,
    subs: list[PlayerPushSubscription],
    payload: WebPushPayload,
    client: WebPushClient,
    ttl_seconds: int,
) -> None:
    """Limit network concurrency and batch expired-subscription updates."""
    data = payload.to_json()

    def deliver(sub: PlayerPushSubscription) -> PlayerPushSubscription | None:
        try:
            client.send(
                subscription=sub.subscription, data=data, ttl_seconds=ttl_seconds
            )
        except WebPushDeliveryError as exc:
            if exc.status_code in {404, 410}:
                return sub
            logger.warning("Web push delivery failed", exc_info=True)
        except Exception:
            logger.warning("Web push delivery failed", exc_info=True)
        return None

    with ThreadPoolExecutor(max_workers=WEB_PUSH_WORKERS) as executor:
        for batch in batched(subs, 100):
            expired = [
                sub.pk for sub in executor.map(deliver, batch) if sub is not None
            ]
            if expired:
                PlayerPushSubscription.objects.filter(pk__in=expired).update(
                    is_active=False, updated_at=timezone.now()
                )
