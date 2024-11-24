"""Admin settings for the MatchPlayer model."""

from django.contrib import admin

from ..models import MatchPlayer


class MatchPlayerAdmin(admin.ModelAdmin):
    """Admin for the MatchPlayer model."""

    list_display = ["id_uuid", "match_data", "team", "player"]
    show_full_result_count = False

    class Meta:
        """Meta class for the MatchPlayerAdmin."""

        model = MatchPlayer


admin.site.register(MatchPlayer, MatchPlayerAdmin)
