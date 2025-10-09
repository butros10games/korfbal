"""Admin settings for the MatchPart model."""

from typing import TYPE_CHECKING

from django.contrib import admin

from apps.game_tracker.models import MatchPart


if TYPE_CHECKING:
    from django.contrib.admin import ModelAdmin as ModelAdminBase

    MatchPartAdminBase = ModelAdminBase[MatchPart]
else:
    MatchPartAdminBase = admin.ModelAdmin


class MatchPartAdmin(MatchPartAdminBase):
    """Admin for the MatchPart model."""

    list_display = ["id_uuid", "start_time", "end_time", "match_data"]  # noqa: RUF012
    show_full_result_count = False

    class Meta:
        """Meta class for the MatchPartAdmin."""

        model = MatchPart


admin.site.register(MatchPart, MatchPartAdmin)
