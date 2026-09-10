"""Admin for the Shot model."""

from collections.abc import Mapping, MutableMapping
from typing import TYPE_CHECKING, Any

from django import forms
from django.contrib import admin
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import UploadedFile
from django.forms.renderers import BaseRenderer
from django.forms.utils import ErrorList
from django.utils.datastructures import MultiValueDict

from apps.game_tracker.models import Shot
from apps.kwt_common.admin_base import KorfbalModelAdmin


if TYPE_CHECKING:
    from apps.kwt_common.admin_base import KorfbalModelAdmin as ModelAdminBase

    ShotAdminBase = ModelAdminBase[Shot]
else:
    ShotAdminBase = KorfbalModelAdmin


class ShotAdminForm(forms.ModelForm):
    """Form for the ShotAdmin."""

    class Meta:
        """Meta class for the ShotAdminForm."""

        model = Shot
        fields = ["player", "match_data", "for_team", "team", "scored"]

    def __init__(
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
        from apps.game_tracker.models import MatchData
        from apps.team.models import Team

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

        match_data_id = (
            self.data.get(self.add_prefix("match_data"))
            if self.is_bound
            else getattr(instance, "match_data_id", None)
            or self.initial.get("match_data")
        )
        try:
            match_data = (
                MatchData.objects
                .select_related("match_link")
                .filter(pk=match_data_id)
                .first()
            )
        except (ValueError, ValidationError):
            match_data = None
        team_field.queryset = (
            Team.objects.filter(
                pk__in=[
                    match_data.match_link.home_team_id,
                    match_data.match_link.away_team_id,
                ]
            )
            if match_data
            else Team.objects.none()
        )


@admin.register(Shot)
class ShotAdmin(ShotAdminBase):
    """Admin for the Shot model."""

    list_select_related = (
        "player__user",
        "match_data__match_link__home_team",
        "match_data__match_link__away_team",
        "team__club",
        "match_part__match_data__match_link__home_team",
        "match_part__match_data__match_link__away_team",
        "shot_type",
    )
    search_fields = (
        "id_uuid",
        "player__name",
        "player__user__username",
        "match_data__id_uuid",
        "match_data__match_link__home_team__name",
        "match_data__match_link__away_team__name",
        "team__name",
    )
    list_filter = ("scored", "for_team", "time")
    ordering = ("-time",)

    form = ShotAdminForm
    list_display = ("time", "player", "match_data", "team", "for_team", "scored")
    show_full_result_count = False

    class Meta:
        """Meta class for the ShotAdmin."""

        model = Shot
