"""Admin for the Shot model."""

from django import forms
from django.contrib import admin
from django.db.models import Q

from ..models import Shot


class ShotAdminForm(forms.ModelForm):
    """Form for the ShotAdmin."""

    class Meta:
        """Meta class for the ShotAdminForm."""

        model = Shot
        fields = ["player", "match_data", "for_team", "team", "scored"]

    def __init__(self, *args, **kwargs):
        """Initialize the ShotAdminForm."""
        from apps.team.models import Team

        super(ShotAdminForm, self).__init__(*args, **kwargs)
        if "instance" in kwargs and kwargs["instance"]:
            match = kwargs["instance"].match_data.match_link
            self.fields["team"].queryset = Team.objects.filter(
                Q(home_matches=match) | Q(away_matches=match)
            ).distinct()
        else:
            self.fields["team"].queryset = Team.objects.none()


class ShotAdmin(admin.ModelAdmin):
    """Admin for the Shot model."""

    form = ShotAdminForm
    list_display = ["id_uuid", "player", "match_data", "for_team", "team", "scored"]
    show_full_result_count = False

    class Meta:
        """Meta class for the ShotAdmin."""

        model = Shot


admin.site.register(Shot, ShotAdmin)
