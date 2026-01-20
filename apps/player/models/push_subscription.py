"""Web push subscription models.

The korfbal-web PWA can subscribe to web push notifications (VAPID) and will
send a PushSubscription payload to the backend. We store these subscriptions per
user so we can fan out notifications to all active devices.

We intentionally store the full subscription JSON (endpoint + keys) so the
sender can pass it directly to the web push library.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, ClassVar

from bg_uuidv7 import uuidv7
from django.conf import settings
from django.db import models


class PlayerPushSubscription(models.Model):
    """A single device/browser web push subscription for a user."""

    id_uuid: models.UUIDField[str, str] = models.UUIDField(
        primary_key=True,
        default=uuidv7,
        editable=False,
    )

    user: models.ForeignKey[Any, Any] = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="push_subscriptions",
    )

    endpoint: models.URLField[str, str] = models.URLField(max_length=1024, unique=True)

    subscription: models.JSONField[dict[str, Any], dict[str, Any]] = models.JSONField()

    platform: models.CharField[str, str] = models.CharField(
        max_length=16,
        default="web",
    )

    is_active: models.BooleanField[bool, bool] = models.BooleanField(default=True)

    # Best-effort metadata for debugging/cleanup.
    user_agent: models.TextField[str, str] = models.TextField(blank=True, default="")

    created_at: models.DateTimeField[datetime, datetime] = models.DateTimeField(
        auto_now_add=True,
    )
    updated_at: models.DateTimeField[datetime, datetime] = models.DateTimeField(
        auto_now=True,
    )

    class Meta:
        """Model metadata."""

        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["user", "is_active"], name="push_user_active_idx"),
        ]

    # Django creates `<fk_field>_id` attributes dynamically.
    user_id: int

    def __str__(self) -> str:
        """Return a readable label for admin/debug output."""
        return f"Push subscription {self.id_uuid} (user={self.user_id})"
