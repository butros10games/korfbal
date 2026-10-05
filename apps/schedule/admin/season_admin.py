"""Admin class for the Season model."""

from typing import TYPE_CHECKING, Any

from django.contrib import admin
from django.core.exceptions import ValidationError
from django.forms import ModelForm
from django.http import HttpRequest

from apps.kwt_common.admin_base import KorfbalModelAdmin
from apps.schedule.models import Season


if TYPE_CHECKING:
    from apps.kwt_common.admin_base import KorfbalModelAdmin as ModelAdminBase

    SeasonAdminBase = ModelAdminBase[Season]
else:
    SeasonAdminBase = KorfbalModelAdmin


class SeasonCoverageForm(ModelForm):
    """Require a review explanation before declaring full schedule coverage."""

    class Meta:
        """Expose editable season context and reviewed coverage fields."""

        model = Season
        fields = (
            "name",
            "start_date",
            "end_date",
            "edition",
            "discipline",
            "phase",
            "context_source",
            "data_unavailable",
            "data_coverage",
            "coverage_reason",
        )

    def clean(self) -> dict[str, Any]:
        """Keep a complete status tied to an explicit administrator review.

        Raises:
            ValidationError: Complete coverage lacks an administrator explanation.

        """
        cleaned = super().clean() or {}
        if cleaned.get("data_coverage") == "complete" and not cleaned.get(
            "coverage_reason"
        ):
            raise ValidationError(
                "Complete source coverage requires an explanation of reviewed "
                "schedule proof."
            )
        return cleaned


@admin.register(Season)
class SeasonAdmin(SeasonAdminBase):
    """Admin settings for the Season model."""

    ordering = ("-start_date",)
    form = SeasonCoverageForm

    list_display = (
        "name",
        "start_date",
        "end_date",
        "edition",
        "discipline",
        "phase",
        "context_source",
        "data_coverage",
        "data_unavailable",
    )
    list_filter = (
        "discipline",
        "phase",
        "context_source",
        "data_coverage",
        "data_unavailable",
    )
    search_fields = ("id_uuid", "name")
    show_full_result_count = False

    def save_model(
        self, request: HttpRequest, obj: Season, form: ModelForm, change: bool
    ) -> None:
        """Record an administrator's context edit as a manual decision."""
        if {"edition", "discipline", "phase"} & set(form.changed_data):
            obj.context_source = "manual"
        if "data_coverage" in form.changed_data:
            obj.data_unavailable = obj.data_coverage == "unavailable"
        super().save_model(request, obj, form, change)

    class Meta:
        """Meta class for the Season model."""

        model = Season
