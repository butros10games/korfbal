"""Admin settings for the PlayerChange model."""

from django.contrib import admin

from ..models import PlayerChange


class PlayerChangeAdmin(admin.ModelAdmin):
    """Admin for the PlayerChange model."""

    list_display = ["id_uuid", "player_in", "player_out", "player_group"]
    show_full_result_count = False

    class Meta:
        """Meta class for the PlayerChangeAdmin."""

        model = PlayerChange


admin.site.register(PlayerChange, PlayerChangeAdmin)
