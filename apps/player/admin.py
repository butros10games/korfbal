from django.contrib import admin

from .models import Player

class player_admin(admin.ModelAdmin):
    list_display = ["id_uuid", "user"]
    show_full_result_count = False
    
    class Meta:
        model = Player
admin.site.register(Player, player_admin)