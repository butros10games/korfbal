"""Admin for the Shot model."""

from collections.abc import Mapping, MutableMapping
from typing import TYPE_CHECKING, Any

from django import forms
from django.contrib import admin
from django.core.files.uploadedfile import UploadedFile
from django.db.models import Q
from django.forms.renderers import BaseRenderer
from django.forms.utils import ErrorList
from django.utils.datastructures import MultiValueDict

from apps.game_tracker.models import Shot


if TYPE_CHECKING:
    from django.contrib.admin import ModelAdmin as ModelAdminBase

    ShotAdminBase = ModelAdminBase[Shot]
else:
    ShotAdminBase = admin.ModelAdmin


class ShotAdminForm(forms.ModelForm):  # type: ignore[type-arg]
    """Form for the ShotAdmin."""

    class Meta:
        """Meta class for the ShotAdminForm."""

        model = Shot
        fields = ["player", "match_data", "for_team", "team", "scored"]  # noqa: RUF012

    def __init__(  # noqa: PLR0913, PLR0917
        self,
        data: Mapping[str, Any] | None = None,
        files: MultiValueDict[str, UploadedFile] | None = None,
        auto_id: bool | str = "id_%s",
        prefix: str | None = None,
        initial: MutableMapping[str, Any] | None = None,
        error_class: type[ErrorList] = ErrorList,
        label_suffix: str | None = None,
        empty_permitted: bool = False,
        instance: Shot | None = None,
        use_required_attribute: bool | None = None,
        renderer: BaseRenderer | None = None,
    ) -> None:
        """Initialize the ShotAdminForm."""
        from apps.team.models import Team  # noqa: PLC0415

        super().__init__(
            data=data,
            files=files,
            auto_id=auto_id,
            prefix=prefix,
            initial=initial,
            error_class=error_class,
            label_suffix=label_suffix,
            empty_permitted=empty_permitted,
            instance=instance,
            use_required_attribute=use_required_attribute,
            renderer=renderer,
        )

        team_field = self.fields["team"]
        if not isinstance(team_field, forms.ModelChoiceField):
            return

        if instance is not None:
            match = instance.match_data.match_link
            team_field.queryset = Team.objects.filter(
                Q(home_matches=match) | Q(away_matches=match),
            ).distinct()
        else:
            team_field.queryset = Team.objects.none()


class ShotAdmin(ShotAdminBase):
    """Admin for the Shot model."""

    form = ShotAdminForm
    list_display = (
        "id_uuid",
        "player",
        "match_data",
        "for_team",
        "team",
        "scored",
    )
    show_full_result_count = False

    class Meta:
        """Meta class for the ShotAdmin."""

        model = Shot


admin.site.register(Shot, ShotAdmin)
