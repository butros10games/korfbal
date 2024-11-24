"""Admin class for the Club model."""

from django.contrib import admin

from ..models import Club


class ClubAdmin(admin.ModelAdmin):
    """Admin class for the Club model."""

    list_display = ["id_uuid", "name"]
    show_full_result_count = False

    class Meta:
        """Meta class for the ClubAdmin."""

        model = Club


admin.site.register(Club, ClubAdmin)
