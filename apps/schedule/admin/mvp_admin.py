"""Admin settings for Match MVP voting models."""

from typing import TYPE_CHECKING

from django.contrib import admin

from apps.schedule.models import MatchMvp, MatchMvpVote


if TYPE_CHECKING:
    from django.contrib.admin import ModelAdmin as ModelAdminBase

    MatchMvpAdminBase = ModelAdminBase[MatchMvp]
    MatchMvpVoteAdminBase = ModelAdminBase[MatchMvpVote]
else:
    MatchMvpAdminBase = admin.ModelAdmin
    MatchMvpVoteAdminBase = admin.ModelAdmin


class MatchMvpAdmin(MatchMvpAdminBase):
    """Admin for MatchMvp."""

    list_display = (
        "id_uuid",
        "match",
        "finished_at",
        "closes_at",
        "mvp_player",
        "published_at",
    )
    list_filter = ("published_at", "closes_at")
    search_fields = (
        "id_uuid",
        "match__id_uuid",
        "match__match_data_id",
        "mvp_player__user__username",
    )
    autocomplete_fields = ("match", "mvp_player")
    date_hierarchy = "closes_at"
    show_full_result_count = False

    class Meta:
        """Meta class."""

        model = MatchMvp


class MatchMvpVoteAdmin(MatchMvpVoteAdminBase):
    """Admin for MatchMvpVote."""

    list_display = (
        "id_uuid",
        "match",
        "voter",
        "voter_token",
        "candidate",
        "created_at",
    )
    list_filter = ("created_at", "match")
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


admin.site.register(MatchMvp, MatchMvpAdmin)
admin.site.register(MatchMvpVote, MatchMvpVoteAdmin)
