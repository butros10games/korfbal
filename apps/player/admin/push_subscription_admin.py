"""Admin settings for PlayerPushSubscription."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.contrib import admin

from apps.player.models.push_subscription import PlayerPushSubscription


if TYPE_CHECKING:
    from django.contrib.admin import ModelAdmin as ModelAdminBase

    PlayerPushSubscriptionAdminBase = ModelAdminBase[PlayerPushSubscription]
else:
    PlayerPushSubscriptionAdminBase = admin.ModelAdmin


class PlayerPushSubscriptionAdmin(PlayerPushSubscriptionAdminBase):
    """Admin for web push subscriptions."""

    list_display = (
        "id_uuid",
        "user",
        "is_active",
        "created_at",
        "updated_at",
    )
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


admin.site.register(PlayerPushSubscription, PlayerPushSubscriptionAdmin)
