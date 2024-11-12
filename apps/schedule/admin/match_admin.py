from django.contrib import admin

from ..models import Match


class MatchAdmin(admin.ModelAdmin):
    list_display = ["id_uuid", "home_team", "away_team", "start_time"]
    show_full_result_count = False
    
    class Meta:
        model = Match

admin.site.register(Match, MatchAdmin)
