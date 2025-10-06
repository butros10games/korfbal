"""Admin settings for the Pause model."""

from django.contrib import admin

from apps.game_tracker.models import Pause


class PauseAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    """Admin for the Pause model."""

    list_display = ["id_uuid", "match_data"]  # noqa: RUF012
    show_full_result_count = False

    class Meta:
        """Meta class for the PauseAdmin."""

        model = Pause


admin.site.register(Pause, PauseAdmin)
