"""Admin class for the Season model."""

from django.contrib import admin

from apps.schedule.models import Season


class SeasonAdmin(admin.ModelAdmin):
    """Admin settings for the Season model."""

    list_display = ["id_uuid", "name"]
    show_full_result_count = False

    class Meta:
        """Meta class for the Season model."""

        model = Season


admin.site.register(Season, SeasonAdmin)
