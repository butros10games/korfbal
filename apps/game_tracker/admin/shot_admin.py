"""Admin for the Shot model."""

from typing import ClassVar, cast

from django import forms
from django.contrib import admin
from django.db.models import Q

from apps.game_tracker.models import Shot


class ShotAdminForm(forms.ModelForm):
    """Form for the ShotAdmin."""

    class Meta:
        """Meta class for the ShotAdminForm."""

        model = Shot
        fields = ["player", "match_data", "for_team", "team", "scored"]  # noqa: RUF012

    def __init__(self, *args: object, **kwargs: object) -> None:
        """Initialize the ShotAdminForm."""
        from apps.team.models import Team  # noqa: PLC0415

        super().__init__(*args, **kwargs)
        if kwargs.get("instance"):
            instance = cast(Shot, kwargs["instance"])
            match = instance.match_data.match_link
            self.fields["team"].queryset = Team.objects.filter(  # type: ignore[attr-defined]
                Q(home_matches=match) | Q(away_matches=match),
            ).distinct()
        else:
            self.fields["team"].queryset = Team.objects.none()  # type: ignore[attr-defined]


class ShotAdmin(admin.ModelAdmin):
    """Admin for the Shot model."""

    form = ShotAdminForm
    list_display: ClassVar[list[str]] = [  # type: ignore[assignment,misc]
        "id_uuid",
        "player",
        "match_data",
        "for_team",
        "team",
        "scored",
    ]
    show_full_result_count = False

    class Meta:
        """Meta class for the ShotAdmin."""

        model = Shot


admin.site.register(Shot, ShotAdmin)
