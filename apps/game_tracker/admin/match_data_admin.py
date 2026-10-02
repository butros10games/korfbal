"""Admin settings for the MatchData model."""

from typing import TYPE_CHECKING

from django.contrib import admin
from django.forms import ModelForm
from django.http import HttpRequest

from apps.game_tracker.models import MatchData
from apps.game_tracker.services.match_rule_profiles import mark_manual
from apps.kwt_common.admin_base import KorfbalModelAdmin


if TYPE_CHECKING:
    from apps.kwt_common.admin_base import KorfbalModelAdmin as ModelAdminBase

    MatchDataAdminBase = ModelAdminBase[MatchData]
else:
    MatchDataAdminBase = KorfbalModelAdmin


@admin.register(MatchData)
class MatchDataAdmin(MatchDataAdminBase):
    """Admin for the MatchData model."""

    list_select_related = ("match_link__home_team", "match_link__away_team")
    list_filter = ("status", "score_source", "rules_source")
    ordering = ("-match_link__start_time",)

    list_display = (
        "match_link",
        "home_score",
        "away_score",
        "status",
        "current_part",
        "live_changed_at",
    )
    search_fields = (
        "id_uuid",
        "match_link__id_uuid",
        "match_link__home_team__name",
        "match_link__away_team__name",
    )
    show_full_result_count = False
    readonly_fields = ("rules", "rules_source", "rules_pending")

    def save_model(
        self, request: HttpRequest, obj: MatchData, form: ModelForm, change: bool
    ) -> None:
        """Record a directly configured clock as a manual rule decision."""
        if {"parts", "part_length"} & set(form.changed_data):
            mark_manual(obj)
        super().save_model(request, obj, form, change)

    class Meta:
        """Meta class for the MatchDataAdmin."""

        model = MatchData
