"""Admin settings for Match MVP voting models."""

from typing import TYPE_CHECKING

from django.contrib import admin

from apps.awards.models import MatchMvp, MatchMvpVote
from apps.kwt_common.admin_base import KorfbalModelAdmin
from apps.kwt_common.admin_filters import relation_filter


if TYPE_CHECKING:
    from apps.kwt_common.admin_base import KorfbalModelAdmin as ModelAdminBase

    MatchMvpAdminBase = ModelAdminBase[MatchMvp]
    MatchMvpVoteAdminBase = ModelAdminBase[MatchMvpVote]
else:
    MatchMvpAdminBase = KorfbalModelAdmin
    MatchMvpVoteAdminBase = KorfbalModelAdmin


@admin.register(MatchMvp)
class MatchMvpAdmin(MatchMvpAdminBase):
    """Admin for MatchMvp."""

    list_select_related = (
        "match__home_team",
        "match__away_team",
        "mvp_player__user",
    )

    list_display = ("match", "finished_at", "closes_at", "mvp_player", "published_at")
    list_filter = ("published_at", "closes_at")
    search_fields = (
        "id_uuid",
        "match__id_uuid",
        "match__home_team__name",
        "match__away_team__name",
        "mvp_player__name",
        "mvp_player__user__username",
    )
    autocomplete_fields = ("match", "mvp_player")
    date_hierarchy = "closes_at"
    show_full_result_count = False

    class Meta:
        """Meta class."""

        model = MatchMvp


@admin.register(MatchMvpVote)
class MatchMvpVoteAdmin(MatchMvpVoteAdminBase):
    """Admin for MatchMvpVote."""

    list_select_related = (
        "match__home_team",
        "match__away_team",
        "voter__user",
        "candidate__user",
    )

    list_display = ("match", "voter", "candidate", "created_at")
    list_filter = ("created_at", relation_filter("match", "Match"))
    search_fields = (
        "id_uuid",
        "match__id_uuid",
        "voter__user__username",
        "voter__user__email",
        "candidate__user__username",
        "candidate__user__email",
    )
    autocomplete_fields = ("match", "voter", "candidate")
    date_hierarchy = "created_at"
    show_full_result_count = False

    class Meta:
        """Meta class."""

        model = MatchMvpVote
