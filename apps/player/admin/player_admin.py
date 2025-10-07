"""Admin configuration for the Player model."""

from typing import TYPE_CHECKING

from django.contrib import admin

from apps.player.models import Player


if TYPE_CHECKING:
    from django.contrib.admin import ModelAdmin as ModelAdminBase

    PlayerModelAdminBase = ModelAdminBase[Player]
else:
    PlayerModelAdminBase = admin.ModelAdmin


class PlayerAdmin(PlayerModelAdminBase):
    """Player admin configuration."""

    list_display = ["id_uuid", "user"]  # noqa: RUF012
    show_full_result_count = False

    class Meta:
        """Meta class."""

        model = Player


admin.site.register(Player, PlayerAdmin)
