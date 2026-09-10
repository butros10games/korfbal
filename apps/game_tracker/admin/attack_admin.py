"""Admin settings for the Attack model."""

from typing import TYPE_CHECKING

from django.contrib import admin

from apps.game_tracker.models import Attack
from apps.kwt_common.admin_base import KorfbalModelAdmin
from apps.kwt_common.admin_filters import relation_filter


if TYPE_CHECKING:
    from apps.kwt_common.admin_base import KorfbalModelAdmin as ModelAdminBase

    AttackAdminBase = ModelAdminBase[Attack]
else:
    AttackAdminBase = KorfbalModelAdmin


@admin.register(Attack)
class AttackAdmin(AttackAdminBase):
    """Admin for the Attack model."""

    list_select_related = (
        "match_data__match_link__home_team",
        "match_data__match_link__away_team",
        "match_part__match_data__match_link__home_team",
        "match_part__match_data__match_link__away_team",
        "team__club",
    )
    ordering = ("-time",)

    list_display = ("match_data", "team", "match_part", "time")
    list_filter = (
        relation_filter("team", "Team"),
        relation_filter("match_part", "Match part"),
    )
    search_fields = (
        "id_uuid",
        "match_data__id_uuid",
        "team__name",
    )
    autocomplete_fields = ("match_data", "match_part", "team")
    show_full_result_count = False

    class Meta:
        """Meta class."""

        model = Attack
