"""Admin settings for the MatchPart model."""

from django.contrib import admin

from ..models import MatchPart


class MatchPartAdmin(admin.ModelAdmin):
    """Admin for the MatchPart model."""

    list_display = ["id_uuid", "start_time", "end_time", "match_data"]
    show_full_result_count = False

    class Meta:
        """Meta class for the MatchPartAdmin."""

        model = MatchPart


admin.site.register(MatchPart, MatchPartAdmin)
