"""Admin settings for the Pause model."""

from django.contrib import admin

from ..models import Pause


class PauseAdmin(admin.ModelAdmin):
    """Admin for the Pause model."""

    list_display = ["id_uuid", "match_data"]
    show_full_result_count = False

    class Meta:
        """Meta class for the PauseAdmin."""

        model = Pause


admin.site.register(Pause, PauseAdmin)
