from django.contrib import admin

from ..models import MatchPlayer


class MatchPlayerAdmin(admin.ModelAdmin):
    list_display = ["id_uuid", "match_data", "team", "player"]
    show_full_result_count = False

    class Meta:
        model = MatchPlayer


admin.site.register(MatchPlayer, MatchPlayerAdmin)
