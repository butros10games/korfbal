"""Admin configuration for the Player model."""

from typing import TYPE_CHECKING

from django.contrib import admin

from apps.kwt_common.admin_base import KorfbalModelAdmin
from apps.player.models import Player, PlayerClubMembership, PlayerSong


if TYPE_CHECKING:
    from django.contrib.admin import (
        TabularInline as TabularInlineBase,
    )

    from apps.kwt_common.admin_base import KorfbalModelAdmin as ModelAdminBase

    PlayerModelAdminBase = ModelAdminBase[Player]
    PlayerSongInlineBase = TabularInlineBase[PlayerSong, Player]
    PlayerClubMembershipInlineBase = TabularInlineBase[PlayerClubMembership, Player]
else:
    PlayerModelAdminBase = KorfbalModelAdmin
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

    autocomplete_fields = ("club",)

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


@admin.register(Player)
class PlayerAdmin(PlayerModelAdminBase):
    """Player admin configuration."""

    list_select_related = ("user",)
    list_filter = ("archived_at", "knkv_privacy")
    ordering = ("name", "id_uuid")

    list_display = ("display_name", "user", "knkv_observed_at")
    search_fields = (
        "id_uuid",
        "name",
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
