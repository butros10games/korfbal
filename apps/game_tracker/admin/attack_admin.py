"""Admin settings for the Attack model."""

from typing import TYPE_CHECKING, ClassVar

from django.contrib import admin

from apps.game_tracker.models import Attack


if TYPE_CHECKING:
    from django.contrib.admin import ModelAdmin as ModelAdminBase

    AttackAdminBase = ModelAdminBase[Attack]
else:
    AttackAdminBase = admin.ModelAdmin


class AttackAdmin(AttackAdminBase):
    """Admin for the Attack model."""

    list_display: ClassVar[list[str]] = [
        "id_uuid",
        "match_data",
        "team",
        "match_part",
        "time",
    ]
    list_filter: ClassVar[list[str]] = ["team", "match_part"]
    search_fields: ClassVar[list[str]] = [
        "id_uuid",
        "match_data__id_uuid",
        "team__name",
    ]
    autocomplete_fields: ClassVar[list[str]] = ["match_data", "match_part", "team"]
    show_full_result_count = False

    class Meta:
        """Meta class."""

        model = Attack


admin.site.register(Attack, AttackAdmin)
