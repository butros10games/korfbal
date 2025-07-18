"""Admin configuration for the Player model."""

from django.contrib import admin

from apps.player.models import Player


class PlayerAdmin(admin.ModelAdmin):
    """Player admin configuration."""

    list_display = ["id_uuid", "user"]
    show_full_result_count = False

    class Meta:
        """Meta class."""

        model = Player


admin.site.register(Player, PlayerAdmin)
