from django.contrib import admin

from ..models import Pause


class PauseAdmin(admin.ModelAdmin):
    list_display = ["id_uuid", "match_data"]
    show_full_result_count = False
    
    class Meta:
        model = Pause

admin.site.register(Pause, PauseAdmin)
