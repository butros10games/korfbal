from django.contrib import admin
from .models import Club, Team, TeamData, Player, Match, PlayerGroup, GroupTypes, PlayerChange, GoalType, Pause, Season, MatchPart, Shot, PageConnectRegistration

from django import forms
from django.db.models import Q

# Register your models here.
class club_admin(admin.ModelAdmin):
    list_display = ["id_uuid", "name"]
    show_full_result_count = False
    
    class Meta:
        model = Club
admin.site.register(Club, club_admin)

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

class player_admin(admin.ModelAdmin):
    list_display = ["id_uuid", "user"]
    show_full_result_count = False
    
    class Meta:
        model = Player
admin.site.register(Player, player_admin)

class match_admin(admin.ModelAdmin):
    list_display = ["id_uuid", "home_team", "away_team", "home_score", "away_score", "start_time", "part_lenght", "active", "finished"]
    show_full_result_count = False
    
    class Meta:
        model = Match
admin.site.register(Match, match_admin)

class match_part_admin(admin.ModelAdmin):
    list_display = ["id_uuid", "start_time", "end_time", "active", "match"]
    show_full_result_count = False
    
    class Meta:
        model = MatchPart
admin.site.register(MatchPart, match_part_admin)

class player_group_admin(admin.ModelAdmin):
    list_display = ["id_uuid", "team", "match", "starting_type", "current_type"]
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
        fields = '__all__'

    def __init__(self, *args, **kwargs):
        super(ShotAdminForm, self).__init__(*args, **kwargs)
        if 'instance' in kwargs and kwargs['instance']:
            match = kwargs['instance'].match
            self.fields['team'].queryset = Team.objects.filter(
                Q(home_matches=match) | Q(away_matches=match)
            ).distinct()
        else:
            self.fields['team'].queryset = Team.objects.none()

class ShotAdmin(admin.ModelAdmin):
    form = ShotAdminForm
    list_display = ["id_uuid", "player", "match", "for_team", "team", "scored"]
    show_full_result_count = False

    class Meta:
        model = Shot

admin.site.register(Shot, ShotAdmin)

class pause_admin(admin.ModelAdmin):
    list_display = ["id_uuid", "match"]
    show_full_result_count = False
    
    class Meta:
        model = Pause
admin.site.register(Pause, pause_admin)

class season_admin(admin.ModelAdmin):
    list_display = ["id_uuid", "name"]
    show_full_result_count = False
    
    class Meta:
        model = Season
admin.site.register(Season, season_admin)

class page_connect_registration_admin(admin.ModelAdmin):
    list_display = ["id_uuid", "player", "page", "registration_date"]
    show_full_result_count = False
    
    class Meta:
        model = PageConnectRegistration
admin.site.register(PageConnectRegistration, page_connect_registration_admin)
