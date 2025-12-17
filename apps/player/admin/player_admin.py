"""Admin configuration for the Player model."""

from typing import TYPE_CHECKING, ClassVar

from django.contrib import admin

from apps.player.models import Player, PlayerSong


if TYPE_CHECKING:
    from django.contrib.admin import (
        ModelAdmin as ModelAdminBase,
        TabularInline as TabularInlineBase,
    )

    PlayerModelAdminBase = ModelAdminBase[Player]
    PlayerSongInlineBase = TabularInlineBase[PlayerSong, Player]
else:
    PlayerModelAdminBase = admin.ModelAdmin
    PlayerSongInlineBase = admin.TabularInline


class PlayerSongInline(PlayerSongInlineBase):
    """Inline admin showing a player's downloaded songs."""

    model = PlayerSong
    extra = 0
    show_change_link = True

    fields: ClassVar[list[str]] = [
        "id_uuid",
        "status",
        "title",
        "artists",
        "start_time_seconds",
        "created_at",
        "updated_at",
    ]
    readonly_fields: ClassVar[list[str]] = [
        "id_uuid",
        "status",
        "title",
        "artists",
        "created_at",
        "updated_at",
    ]


class PlayerAdmin(PlayerModelAdminBase):
    """Player admin configuration."""

    list_display: ClassVar[list[str]] = ["id_uuid", "user"]
    show_full_result_count = False
    inlines: ClassVar[list[type]] = [PlayerSongInline]

    class Meta:
        """Meta class."""

        model = Player


admin.site.register(Player, PlayerAdmin)
