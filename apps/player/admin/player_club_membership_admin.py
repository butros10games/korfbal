"""Admin settings for the PlayerClubMembership model."""

from typing import TYPE_CHECKING

from django.contrib import admin

from apps.kwt_common.admin_base import KorfbalModelAdmin
from apps.kwt_common.admin_filters import relation_filter
from apps.player.models import PlayerClubMembership


if TYPE_CHECKING:
    from apps.kwt_common.admin_base import KorfbalModelAdmin as ModelAdminBase

    PlayerClubMembershipAdminBase = ModelAdminBase[PlayerClubMembership]
else:
    PlayerClubMembershipAdminBase = KorfbalModelAdmin


@admin.register(PlayerClubMembership)
class PlayerClubMembershipAdmin(PlayerClubMembershipAdminBase):
    """Admin configuration for PlayerClubMembership."""

    list_select_related = ("player__user", "club")

    list_display = ("player", "club", "start_date", "end_date")
    list_filter = (relation_filter("club", "Club"), "start_date", "end_date")
    search_fields = (
        "id_uuid",
        "player__name",
        "player__user__username",
        "player__user__email",
        "club__name",
    )
    autocomplete_fields = ("player", "club")
    date_hierarchy = "start_date"
    show_full_result_count = False

    class Meta:
        """Meta class."""

        model = PlayerClubMembership
