from django.contrib import admin
from django import forms
from django.db.models import Q

from ..models import Shot


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
