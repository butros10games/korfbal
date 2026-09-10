"""Admin configuration for PlayerSong."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.contrib import admin

from apps.kwt_common.admin_base import KorfbalModelAdmin
from apps.player.models import PlayerSong


if TYPE_CHECKING:
    from apps.kwt_common.admin_base import KorfbalModelAdmin as ModelAdminBase

    PlayerSongModelAdminBase = ModelAdminBase[PlayerSong]
else:
    PlayerSongModelAdminBase = KorfbalModelAdmin


@admin.register(PlayerSong)
class PlayerSongAdmin(PlayerSongModelAdminBase):
    """PlayerSong admin configuration."""

    list_select_related = ("player__user", "cached_song")
    ordering = ("-created_at",)

    list_display = (
        "title",
        "player",
        "status",
        "artists",
        "duration_seconds",
        "created_at",
    )

    list_filter = ("status", "created_at")
    search_fields = (
        "id_uuid",
        "spotify_url",
        "title",
        "artists",
        "player__name",
        "player__user__username",
        "player__user__email",
    )

    readonly_fields = (
        "id_uuid",
        "created_at",
        "updated_at",
    )

    fieldsets = (
        (
            None,
            {
                "fields": (
                    "id_uuid",
                    "player",
                    "spotify_url",
                    "status",
                )
            },
        ),
        (
            "Metadata",
            {
                "fields": (
                    "title",
                    "artists",
                    "duration_seconds",
                    "start_time_seconds",
                    "error_message",
                )
            },
        ),
        (
            "Audio",
            {
                "fields": ("audio_file",),
            },
        ),
        (
            "Timestamps",
            {
                "fields": (
                    "created_at",
                    "updated_at",
                )
            },
        ),
    )

    show_full_result_count = False

    class Meta:
        """Meta class."""

        model = PlayerSong
