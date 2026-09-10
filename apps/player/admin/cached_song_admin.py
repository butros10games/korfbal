"""Admin settings for the CachedSong model."""

from typing import TYPE_CHECKING

from django.contrib import admin

from apps.kwt_common.admin_base import KorfbalModelAdmin
from apps.player.models import CachedSong


if TYPE_CHECKING:
    from apps.kwt_common.admin_base import KorfbalModelAdmin as ModelAdminBase

    CachedSongAdminBase = ModelAdminBase[CachedSong]
else:
    CachedSongAdminBase = KorfbalModelAdmin


@admin.register(CachedSong)
class CachedSongAdmin(CachedSongAdminBase):
    """Admin configuration for CachedSong."""

    ordering = ("-updated_at",)

    list_display = ("title", "status", "artists", "created_at", "updated_at")
    list_filter = ("status", "created_at")
    search_fields = ("id_uuid", "spotify_url", "title", "artists")
    readonly_fields = ("created_at", "updated_at")
    show_full_result_count = False

    class Meta:
        """Meta class."""

        model = CachedSong
