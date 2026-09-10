"""Admin settings for PlayerPushSubscription."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.contrib import admin

from apps.kwt_common.admin_base import KorfbalModelAdmin
from apps.player.models.push_subscription import PlayerPushSubscription


if TYPE_CHECKING:
    from apps.kwt_common.admin_base import KorfbalModelAdmin as ModelAdminBase

    PlayerPushSubscriptionAdminBase = ModelAdminBase[PlayerPushSubscription]
else:
    PlayerPushSubscriptionAdminBase = KorfbalModelAdmin


@admin.register(PlayerPushSubscription)
class PlayerPushSubscriptionAdmin(PlayerPushSubscriptionAdminBase):
    """Admin for web push subscriptions."""

    list_select_related = ("user",)
    ordering = ("-updated_at",)

    list_display = ("user", "is_active", "created_at", "updated_at")
    list_filter = ("is_active", "created_at")
    search_fields = (
        "id_uuid",
        "user__username",
        "user__email",
        "endpoint",
    )
    readonly_fields = ("created_at", "updated_at")
    show_full_result_count = False

    class Meta:
        """Meta options."""

        model = PlayerPushSubscription
