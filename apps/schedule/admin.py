from django.contrib import admin

from .models import Season, Match

class season_admin(admin.ModelAdmin):
    list_display = ["id_uuid", "name"]
    show_full_result_count = False
    
    class Meta:
        model = Season
admin.site.register(Season, season_admin)

class match_admin(admin.ModelAdmin):
    list_display = ["id_uuid", "home_team", "away_team", "start_time"]
    show_full_result_count = False
    
    class Meta:
        model = Match
admin.site.register(Match, match_admin)
