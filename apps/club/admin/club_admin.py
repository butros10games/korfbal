"""Admin configuration for club-related models."""

from typing import TYPE_CHECKING

from django.contrib import admin
from django.core.exceptions import ObjectDoesNotExist
from django.db.models import QuerySet
from django.http import HttpRequest
from django.urls import reverse
from django.utils.html import format_html

from apps.club.models import Club, ClubAdmin


if TYPE_CHECKING:
    from django.contrib.admin import ModelAdmin as ModelAdminBase

    ClubModelAdminBase = ModelAdminBase[Club]
    ClubAdminLinkAdminBase = ModelAdminBase[ClubAdmin]
else:
    ClubModelAdminBase = admin.ModelAdmin
    ClubAdminLinkAdminBase = admin.ModelAdmin


@admin.register(Club)
class ClubModelAdmin(ClubModelAdminBase):
    """Admin configuration for the Club model."""

    list_display = ("id_uuid", "name", "knkv_catalogue")
    search_fields = ("name", "id_uuid")
    show_full_result_count = False

    def get_queryset(self, request: HttpRequest) -> QuerySet[Club]:
        """Load source links in the same query as the existing club list."""
        return super().get_queryset(request).select_related("competition_identity")

    @admin.display(description="KNKV catalogue")
    def knkv_catalogue(self, obj: Club) -> str:
        """Link the existing club to its imported catalogue record."""
        try:
            source = obj.competition_identity
        except ObjectDoesNotExist:
            return "—"
        return format_html(
            '<a href="{}">{}</a>',
            reverse("admin:competition_club_change", args=(source.pk,)),
            source.name,
        )

    class Meta:
        """Meta class for the ClubModelAdmin."""

        model = Club


@admin.register(ClubAdmin)
class ClubAdminLinkAdmin(ClubAdminLinkAdminBase):
    """Admin configuration for the ClubAdmin through model."""

    list_display = ("club", "player")
    search_fields = (
        "club__name",
        "player__user__username",
        "player__user__email",
    )
    autocomplete_fields = ("club", "player")
    show_full_result_count = False

    class Meta:
        """Meta class for the ClubAdminLinkAdmin."""

        model = ClubAdmin
