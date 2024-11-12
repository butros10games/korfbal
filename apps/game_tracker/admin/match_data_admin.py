from django.contrib import admin

from ..models import MatchData


class MatchDataAdmin(admin.ModelAdmin):
    list_display = ["id_uuid", "__str__", "home_score", "away_score", "part_lenght", "status"]
    show_full_result_count = False
    
    class Meta:
        model = MatchData

admin.site.register(MatchData, MatchDataAdmin)
