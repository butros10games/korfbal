"""Admin class for the PlayerGroup model."""

from django.contrib import admin

from apps.game_tracker.models import PlayerGroup


class PlayerGroupAdmin(admin.ModelAdmin):
    """Admin for the PlayerGroup model."""

    list_display = ["id_uuid", "team", "match_data", "starting_type", "current_type"]
    show_full_result_count = False

    class Meta:
        """Meta class for the PlayerGroupAdmin."""

        model = PlayerGroup


admin.site.register(PlayerGroup, PlayerGroupAdmin)
