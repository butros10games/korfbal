"""Admin configuration for the Player model."""

from typing import TYPE_CHECKING

from django.contrib import admin

from apps.player.models import Player, PlayerClubMembership, PlayerSong


if TYPE_CHECKING:
    from django.contrib.admin import (
        ModelAdmin as ModelAdminBase,
        TabularInline as TabularInlineBase,
    )

    PlayerModelAdminBase = ModelAdminBase[Player]
    PlayerSongInlineBase = TabularInlineBase[PlayerSong, Player]
    PlayerClubMembershipInlineBase = TabularInlineBase[PlayerClubMembership, Player]
else:
    PlayerModelAdminBase = admin.ModelAdmin
    PlayerSongInlineBase = admin.TabularInline
    PlayerClubMembershipInlineBase = admin.TabularInline


class PlayerSongInline(PlayerSongInlineBase):
    """Inline admin showing a player's downloaded songs."""

    model = PlayerSong
    extra = 0
    show_change_link = True

    fields = (
        "id_uuid",
        "status",
        "title",
        "artists",
        "start_time_seconds",
        "created_at",
        "updated_at",
    )
    readonly_fields = (
        "id_uuid",
        "status",
        "title",
        "artists",
        "created_at",
        "updated_at",
    )


class PlayerClubMembershipInline(PlayerClubMembershipInlineBase):
    """Inline admin showing a player's club membership history."""

    model = PlayerClubMembership
    extra = 0
    show_change_link = True

    fields = (
        "id_uuid",
        "club",
        "start_date",
        "end_date",
        "created_at",
        "updated_at",
    )
    readonly_fields = (
        "id_uuid",
        "created_at",
        "updated_at",
    )


class PlayerAdmin(PlayerModelAdminBase):
    """Player admin configuration."""

    list_display = ("id_uuid", "user")
    search_fields = (
        "id_uuid",
        "user__username",
        "user__email",
        "user__first_name",
        "user__last_name",
    )
    autocomplete_fields = ("user",)
    show_full_result_count = False
    inlines = (PlayerClubMembershipInline, PlayerSongInline)

    class Meta:
        """Meta class."""

        model = Player


admin.site.register(Player, PlayerAdmin)
