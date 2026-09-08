"""Browser audience delivery stays bounded and isolates failing devices."""

from threading import Barrier, Lock, get_ident
from unittest.mock import Mock

from django.contrib.auth import get_user_model
import pytest

from apps.player.adapters.outbound.web_push_batch import send_web_push_batch
from apps.player.application.ports import WebPushDeliveryError
from apps.player.models.push_subscription import PlayerPushSubscription
from apps.player.services.web_push import WebPushPayload


def test_browser_delivery_uses_bounded_concurrency_and_attempts_every_device() -> None:
    """Four simultaneous provider calls make progress even when one device fails."""
    workers = 4
    barrier = Barrier(workers, timeout=5)
    lock = Lock()
    threads: set[int] = set()
    attempted: list[str] = []
    subscriptions = [
        PlayerPushSubscription(subscription={"endpoint": str(index)})
        for index in range(8)
    ]

    def deliver(*, subscription: dict, data: str, ttl_seconds: int) -> None:
        with lock:
            threads.add(get_ident())
            attempted.append(subscription["endpoint"])
        barrier.wait()
        if subscription["endpoint"] == "0":
            raise RuntimeError("Synthetic unavailable device")

    send_web_push_batch(
        subs=subscriptions,
        payload=WebPushPayload(title="Schedule", body="Changed", url="/"),
        client=Mock(send=deliver),
        ttl_seconds=60,
    )
    assert len(threads) == workers
    assert sorted(attempted) == [str(index) for index in range(8)]


@pytest.mark.django_db
def test_expired_browser_subscriptions_are_deactivated_after_delivery() -> None:
    """Worker threads do network I/O; expiration updates use the caller's connection."""
    user = get_user_model().objects.create_user(username="push-batch")
    sub = PlayerPushSubscription.objects.create(
        user=user,
        endpoint="https://synthetic.invalid",
        subscription={"endpoint": "https://synthetic.invalid"},
    )
    client = Mock()
    client.send.side_effect = WebPushDeliveryError("gone", status_code=410)
    send_web_push_batch(
        subs=[sub],
        payload=WebPushPayload(title="Schedule", body="Changed", url="/"),
        client=client,
        ttl_seconds=60,
    )
    sub.refresh_from_db()
    assert not sub.is_active
