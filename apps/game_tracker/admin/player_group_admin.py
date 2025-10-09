"""Admin class for the PlayerGroup model."""

from typing import TYPE_CHECKING, ClassVar

from django.contrib import admin

from apps.game_tracker.models import PlayerGroup


if TYPE_CHECKING:
    from django.contrib.admin import ModelAdmin as ModelAdminBase

    PlayerGroupAdminBase = ModelAdminBase[PlayerGroup]
else:
    PlayerGroupAdminBase = admin.ModelAdmin


class PlayerGroupAdmin(PlayerGroupAdminBase):
    """Admin for the PlayerGroup model."""

    list_display: ClassVar[list[str]] = [  # type: ignore[misc,assignment]
        "id_uuid",
        "team",
        "match_data",
        "starting_type",
        "current_type",
    ]
    show_full_result_count = False

    class Meta:
        """Meta class for the PlayerGroupAdmin."""

        model = PlayerGroup


admin.site.register(PlayerGroup, PlayerGroupAdmin)
