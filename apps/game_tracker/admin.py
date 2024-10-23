from django.contrib import admin
from django import forms
from django.db.models import Q

from .models import MatchData, PlayerGroup, GroupTypes, PlayerChange, GoalType, Pause, MatchPart, Shot, MatchPlayer


class match_data_admin(admin.ModelAdmin):
    list_display = ["id_uuid", "__str__", "home_score", "away_score", "part_lenght", "status"]
    show_full_result_count = False
    
    class Meta:
        model = MatchData
admin.site.register(MatchData, match_data_admin)

class match_part_admin(admin.ModelAdmin):
    list_display = ["id_uuid", "start_time", "end_time", "match_data"]
    show_full_result_count = False
    
    class Meta:
        model = MatchPart
admin.site.register(MatchPart, match_part_admin)

class match_player_admin(admin.ModelAdmin):
    list_display = ["id_uuid", "match_data", "team", "player"]
    show_full_result_count = False
    
    class Meta:
        model = MatchPlayer
admin.site.register(MatchPlayer, match_player_admin)

class player_group_admin(admin.ModelAdmin):
    list_display = ["id_uuid", "team", "match_data", "starting_type", "current_type"]
    show_full_result_count = False
    
    class Meta:
        model = PlayerGroup
admin.site.register(PlayerGroup, player_group_admin)

class game_types_admin(admin.ModelAdmin):
    list_display = ["id_uuid", "name"]
    show_full_result_count = False
    
    class Meta:
        model = GroupTypes
admin.site.register(GroupTypes, game_types_admin)

class player_change_admin(admin.ModelAdmin):
    list_display = ["id_uuid", "player_in", "player_out", "player_group"]
    show_full_result_count = False
    
    class Meta:
        model = PlayerChange
admin.site.register(PlayerChange, player_change_admin)

class goal_type_admin(admin.ModelAdmin):
    list_display = ["id_uuid", "name"]
    show_full_result_count = False
    
    class Meta:
        model = GoalType
admin.site.register(GoalType, goal_type_admin)

class ShotAdminForm(forms.ModelForm):
    class Meta:
        model = Shot
        fields = ['player', 'match_data', 'for_team', 'team', 'scored']

    def __init__(self, *args, **kwargs):
        from apps.team.models import Team
        super(ShotAdminForm, self).__init__(*args, **kwargs)
        if 'instance' in kwargs and kwargs['instance']:
            match = kwargs['instance'].match_data
            self.fields['team'].queryset = Team.objects.filter(
                Q(home_matches=match) | Q(away_matches=match)
            ).distinct()
        else:
            self.fields['team'].queryset = Team.objects.none()

class ShotAdmin(admin.ModelAdmin):
    form = ShotAdminForm
    list_display = ["id_uuid", "player", "match_data", "for_team", "team", "scored"]
    show_full_result_count = False

    class Meta:
        model = Shot

admin.site.register(Shot, ShotAdmin)

class pause_admin(admin.ModelAdmin):
    list_display = ["id_uuid", "match_data"]
    show_full_result_count = False
    
    class Meta:
        model = Pause
admin.site.register(Pause, pause_admin)
