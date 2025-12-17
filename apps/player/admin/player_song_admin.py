"""Admin configuration for PlayerSong."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from django.contrib import admin

from apps.player.models import PlayerSong


if TYPE_CHECKING:
    from django.contrib.admin import ModelAdmin as ModelAdminBase

    PlayerSongModelAdminBase = ModelAdminBase[PlayerSong]
else:
    PlayerSongModelAdminBase = admin.ModelAdmin


class PlayerSongAdmin(PlayerSongModelAdminBase):
    """PlayerSong admin configuration."""

    list_display: ClassVar[list[str]] = [
        "id_uuid",
        "player",
        "status",
        "title",
        "artists",
        "duration_seconds",
        "start_time_seconds",
        "created_at",
    ]

    list_filter: ClassVar[list[str]] = ["status", "created_at"]
    search_fields: ClassVar[list[str]] = [
        "id_uuid",
        "spotify_url",
        "title",
        "artists",
        "player__user__username",
        "player__user__email",
    ]

    readonly_fields: ClassVar[list[str]] = [
        "id_uuid",
        "created_at",
        "updated_at",
    ]

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


admin.site.register(PlayerSong, PlayerSongAdmin)
