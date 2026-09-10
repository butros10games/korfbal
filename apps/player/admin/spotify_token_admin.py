"""Admin settings for the SpotifyToken model."""

from typing import TYPE_CHECKING

from django.contrib import admin

from apps.kwt_common.admin_base import KorfbalModelAdmin
from apps.player.models import SpotifyToken


if TYPE_CHECKING:
    from apps.kwt_common.admin_base import KorfbalModelAdmin as ModelAdminBase

    SpotifyTokenAdminBase = ModelAdminBase[SpotifyToken]
else:
    SpotifyTokenAdminBase = KorfbalModelAdmin


@admin.register(SpotifyToken)
class SpotifyTokenAdmin(SpotifyTokenAdminBase):
    """Admin configuration for SpotifyToken."""

    list_select_related = ("user",)
    ordering = ("-expires_at",)

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
