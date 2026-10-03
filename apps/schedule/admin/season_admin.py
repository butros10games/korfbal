"""Admin class for the Season model."""

from typing import TYPE_CHECKING

from django.contrib import admin
from django.forms import ModelForm
from django.http import HttpRequest

from apps.kwt_common.admin_base import KorfbalModelAdmin
from apps.schedule.models import Season


if TYPE_CHECKING:
    from apps.kwt_common.admin_base import KorfbalModelAdmin as ModelAdminBase

    SeasonAdminBase = ModelAdminBase[Season]
else:
    SeasonAdminBase = KorfbalModelAdmin


@admin.register(Season)
class SeasonAdmin(SeasonAdminBase):
    """Admin settings for the Season model."""

    ordering = ("-start_date",)

    list_display = (
        "name",
        "start_date",
        "end_date",
        "edition",
        "discipline",
        "phase",
        "context_source",
        "data_unavailable",
    )
    list_filter = ("discipline", "phase", "context_source", "data_unavailable")
    search_fields = ("id_uuid", "name")
    show_full_result_count = False

    def save_model(
        self, request: HttpRequest, obj: Season, form: ModelForm, change: bool
    ) -> None:
        """Record an administrator's context edit as a manual decision."""
        if {"edition", "discipline", "phase"} & set(form.changed_data):
            obj.context_source = "manual"
        super().save_model(request, obj, form, change)

    class Meta:
        """Meta class for the Season model."""

        model = Season
