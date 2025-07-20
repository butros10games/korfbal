"""Admin settings for the MatchData model."""

from django.contrib import admin

from apps.game_tracker.models import MatchData


class MatchDataAdmin(admin.ModelAdmin):
    """Admin for the MatchData model."""

    list_display = [  # noqa: RUF012
        "id_uuid",
        "__str__",
        "home_score",
        "away_score",
        "part_length",
        "status",
    ]
    show_full_result_count = False

    class Meta:
        """Meta class for the MatchDataAdmin."""

        model = MatchData


admin.site.register(MatchData, MatchDataAdmin)
