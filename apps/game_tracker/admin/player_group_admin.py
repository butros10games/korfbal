from django.contrib import admin

from ..models import PlayerGroup


class PlayerGroupAdmin(admin.ModelAdmin):
    list_display = ["id_uuid", "team", "match_data", "starting_type", "current_type"]
    show_full_result_count = False

    class Meta:
        model = PlayerGroup


admin.site.register(PlayerGroup, PlayerGroupAdmin)
