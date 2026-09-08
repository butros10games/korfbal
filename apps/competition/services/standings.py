"""Bounded official standings queries shared by catalogue read endpoints."""

from django.db.models import Case, F, IntegerField, QuerySet, When
from django.db.models.fields.json import KeyTextTransform
from django.db.models.functions import Cast

from apps.competition.models import PoolEntry


STANDINGS_PAGE_SIZE = 100


def standing_entries() -> QuerySet[PoolEntry]:
    """Sort provider ranks numerically before applying any page boundary."""
    return (
        PoolEntry.objects
        .select_related("team__season", "team__group")
        .annotate(
            official_position=Case(
                When(
                    standing__Position__regex=r"^[0-9]{1,9}$",
                    then=Cast(KeyTextTransform("Position", "standing"), IntegerField()),
                ),
                default=None,
                output_field=IntegerField(),
            )
        )
        .order_by(F("official_position").asc(nulls_last=True), "team_id")
    )
