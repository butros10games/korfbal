"""Admin settings for the PlayerClubMembership model."""

from typing import TYPE_CHECKING, ClassVar

from django.contrib import admin

from apps.player.models import PlayerClubMembership


if TYPE_CHECKING:
    from django.contrib.admin import ModelAdmin as ModelAdminBase

    PlayerClubMembershipAdminBase = ModelAdminBase[PlayerClubMembership]
else:
    PlayerClubMembershipAdminBase = admin.ModelAdmin


class PlayerClubMembershipAdmin(PlayerClubMembershipAdminBase):
    """Admin configuration for PlayerClubMembership."""

    list_display: ClassVar[list[str]] = [
        "id_uuid",
        "player",
        "club",
        "start_date",
        "end_date",
        "created_at",
        "updated_at",
    ]
    list_filter: ClassVar[list[str]] = ["club", "start_date", "end_date"]
    search_fields: ClassVar[list[str]] = [
        "id_uuid",
        "player__user__username",
        "player__user__email",
        "club__name",
    ]
    autocomplete_fields: ClassVar[list[str]] = ["player", "club"]
    date_hierarchy = "start_date"
    show_full_result_count = False

    class Meta:
        """Meta class."""

        model = PlayerClubMembership


admin.site.register(PlayerClubMembership, PlayerClubMembershipAdmin)
