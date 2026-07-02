"""Admin settings for the SpotifyToken model."""

from typing import TYPE_CHECKING

from django.contrib import admin

from apps.player.models import SpotifyToken


if TYPE_CHECKING:
    from django.contrib.admin import ModelAdmin as ModelAdminBase

    SpotifyTokenAdminBase = ModelAdminBase[SpotifyToken]
else:
    SpotifyTokenAdminBase = admin.ModelAdmin


class SpotifyTokenAdmin(SpotifyTokenAdminBase):
    """Admin configuration for SpotifyToken."""

    list_display = ("user", "spotify_user_id", "expires_at")
    search_fields = (
        "user__username",
        "user__email",
        "spotify_user_id",
    )
    list_filter = ("expires_at",)
    autocomplete_fields = ("user",)
    readonly_fields = ("spotify_user_id",)
    show_full_result_count = False

    class Meta:
        """Meta class."""

        model = SpotifyToken


admin.site.register(SpotifyToken, SpotifyTokenAdmin)
