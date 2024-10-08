from django.contrib import admin

from .models import Team, TeamData

class team_admin(admin.ModelAdmin):
    list_display = ["id_uuid", "__str__", "club"]
    show_full_result_count = False
    
    class Meta:
        model = Team
admin.site.register(Team, team_admin)

class team_data_admin(admin.ModelAdmin):
    list_display = ["team", "season"]
    show_full_result_count = False
    
    class Meta:
        model = TeamData
admin.site.register(TeamData, team_data_admin)