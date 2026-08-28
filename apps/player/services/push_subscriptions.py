"""Application services for push-subscription lifecycle commands."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from django.db import transaction

from apps.player.models.push_subscription import PlayerPushSubscription


@dataclass(frozen=True, slots=True)
class PushSubscriptionRegistration:
    """Result of registering or refreshing a push subscription."""

    subscription: PlayerPushSubscription
    created: bool


class PushSubscriptionNotFoundError(Exception):
    """Raised when a user does not own the requested subscription."""


@transaction.atomic
def register_push_subscription(
    *,
    user_id: int,
    subscription: dict[str, Any],
    platform: str,
    user_agent: str,
) -> PushSubscriptionRegistration:
    """Register an endpoint or transfer its ownership to the current user."""
    endpoint = str(subscription.get("endpoint") or "").strip()
    stored, created = PlayerPushSubscription.objects.update_or_create(
        endpoint=endpoint,
        defaults={
            "user_id": user_id,
            "subscription": subscription,
            "platform": platform.strip(),
            "is_active": True,
            "user_agent": user_agent.strip(),
        },
    )
    return PushSubscriptionRegistration(subscription=stored, created=created)


@transaction.atomic
def deactivate_push_subscription(
    *,
    user_id: int,
    endpoint: str | None,
    subscription_id: UUID | None,
) -> None:
    """Deactivate a subscription owned by the specified user.

    Raises:
        PushSubscriptionNotFoundError: If the user does not own a matching record.

    """
    queryset = PlayerPushSubscription.objects.select_for_update().filter(
        user_id=user_id
    )
    if endpoint:
        queryset = queryset.filter(endpoint=endpoint)
    if subscription_id:
        queryset = queryset.filter(id_uuid=subscription_id)

    subscription = queryset.first()
    if subscription is None:
        raise PushSubscriptionNotFoundError
    if subscription.is_active:
        subscription.is_active = False
        subscription.save(update_fields=["is_active", "updated_at"])
