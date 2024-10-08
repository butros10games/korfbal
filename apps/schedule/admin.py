from django.contrib import admin

from .models import Season

class season_admin(admin.ModelAdmin):
    list_display = ["id_uuid", "name"]
    show_full_result_count = False
    
    class Meta:
        model = Season
admin.site.register(Season, season_admin)