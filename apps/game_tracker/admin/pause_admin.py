"""Admin settings for the Pause model."""

from typing import TYPE_CHECKING

from django.contrib import admin

from apps.game_tracker.models import Pause


if TYPE_CHECKING:
    from django.contrib.admin import ModelAdmin as ModelAdminBase

    PauseAdminBase = ModelAdminBase[Pause]
else:
    PauseAdminBase = admin.ModelAdmin


class PauseAdmin(PauseAdminBase):
    """Admin for the Pause model."""

    list_display = ["id_uuid", "match_data"]  # noqa: RUF012
    show_full_result_count = False

    class Meta:
        """Meta class for the PauseAdmin."""

        model = Pause


admin.site.register(Pause, PauseAdmin)
