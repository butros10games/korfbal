"""Admin settings for the Pause model."""

from typing import TYPE_CHECKING, ClassVar

from django.contrib import admin

from apps.game_tracker.models import Pause


if TYPE_CHECKING:
    from django.contrib.admin import ModelAdmin as ModelAdminBase

    PauseAdminBase = ModelAdminBase[Pause]
else:
    PauseAdminBase = admin.ModelAdmin


class PauseAdmin(PauseAdminBase):
    """Admin for the Pause model."""

    list_display: ClassVar[list[str]] = ["id_uuid", "match_data"]
    search_fields: ClassVar[list[str]] = [
        "id_uuid",
        "match_data__id_uuid",
        "match_data__match_link__home_team__name",
        "match_data__match_link__away_team__name",
    ]
    show_full_result_count = False

    class Meta:
        """Meta class for the PauseAdmin."""

        model = Pause


admin.site.register(Pause, PauseAdmin)
