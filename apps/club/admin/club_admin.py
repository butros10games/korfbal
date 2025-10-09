"""Admin class for the Club model."""

from typing import TYPE_CHECKING

from django.contrib import admin

from apps.club.models import Club


if TYPE_CHECKING:
    from django.contrib.admin import ModelAdmin as ModelAdminBase

    ClubAdminBase = ModelAdminBase[Club]
else:
    ClubAdminBase = admin.ModelAdmin


class ClubAdmin(ClubAdminBase):
    """Admin class for the Club model."""

    list_display = ["id_uuid", "name"]  # noqa: RUF012
    show_full_result_count = False

    class Meta:
        """Meta class for the ClubAdmin."""

        model = Club


admin.site.register(Club, ClubAdmin)
