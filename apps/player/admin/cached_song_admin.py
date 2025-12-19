"""Admin settings for the CachedSong model."""

from typing import TYPE_CHECKING, ClassVar

from django.contrib import admin

from apps.player.models import CachedSong


if TYPE_CHECKING:
    from django.contrib.admin import ModelAdmin as ModelAdminBase

    CachedSongAdminBase = ModelAdminBase[CachedSong]
else:
    CachedSongAdminBase = admin.ModelAdmin


class CachedSongAdmin(CachedSongAdminBase):
    """Admin configuration for CachedSong."""

    list_display: ClassVar[list[str]] = [
        "id_uuid",
        "status",
        "title",
        "artists",
        "spotify_url",
        "created_at",
        "updated_at",
    ]
    list_filter: ClassVar[list[str]] = ["status", "created_at"]
    search_fields: ClassVar[list[str]] = ["id_uuid", "spotify_url", "title", "artists"]
    readonly_fields: ClassVar[list[str]] = ["created_at", "updated_at"]
    show_full_result_count = False

    class Meta:
        """Meta class."""

        model = CachedSong


admin.site.register(CachedSong, CachedSongAdmin)
