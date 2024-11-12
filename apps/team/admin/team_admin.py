from django.contrib import admin

from ..models import Team


class TeamAdmin(admin.ModelAdmin):
    list_display = ["id_uuid", "__str__", "club"]
    show_full_result_count = False
    
    class Meta:
        model = Team

admin.site.register(Team, TeamAdmin)
