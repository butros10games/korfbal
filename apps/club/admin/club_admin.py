"""Admin configuration for club-related models."""

from typing import TYPE_CHECKING, ClassVar

from django.contrib import admin

from apps.club.models import Club, ClubAdmin


if TYPE_CHECKING:
    from django.contrib.admin import ModelAdmin as ModelAdminBase

    ClubModelAdminBase = ModelAdminBase[Club]
    ClubAdminLinkAdminBase = ModelAdminBase[ClubAdmin]
else:
    ClubModelAdminBase = admin.ModelAdmin
    ClubAdminLinkAdminBase = admin.ModelAdmin


class ClubModelAdmin(ClubModelAdminBase):
    """Admin configuration for the Club model."""

    list_display: ClassVar[list[str]] = ["id_uuid", "name"]
    search_fields: ClassVar[list[str]] = ["name", "id_uuid"]
    show_full_result_count = False

    class Meta:
        """Meta class for the ClubModelAdmin."""

        model = Club


class ClubAdminLinkAdmin(ClubAdminLinkAdminBase):
    """Admin configuration for the ClubAdmin through model."""

    list_display: ClassVar[list[str]] = ["club", "player"]
    search_fields: ClassVar[list[str]] = [
        "club__name",
        "player__user__username",
        "player__user__email",
    ]
    autocomplete_fields: ClassVar[list[str]] = ["club", "player"]
    show_full_result_count = False

    class Meta:
        """Meta class for the ClubAdminLinkAdmin."""

        model = ClubAdmin


admin.site.register(Club, ClubModelAdmin)
admin.site.register(ClubAdmin, ClubAdminLinkAdmin)
