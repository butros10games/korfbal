from django.contrib import admin

from ..models import PlayerChange


class PlayerChangeAdmin(admin.ModelAdmin):
    list_display = ["id_uuid", "player_in", "player_out", "player_group"]
    show_full_result_count = False

    class Meta:
        model = PlayerChange


admin.site.register(PlayerChange, PlayerChangeAdmin)
