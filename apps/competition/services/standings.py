"""Bounded official standings queries shared by catalogue read endpoints."""

from decimal import Decimal, InvalidOperation
from typing import Any

from django.db.models import Case, F, IntegerField, QuerySet, When
from django.db.models.fields.json import KeyTextTransform
from django.db.models.functions import Cast

from apps.competition.models import PoolEntry


STANDINGS_PAGE_SIZE = 100
MAX_SAFE_INTEGER = 9007199254740991
STANDING_FIELDS = {
    "position": "Position",
    "played": "TotalMatches",
    "won": "Won",
    "drawn": "Draw",
    "lost": "Lost",
    "points": "TotalPoints",
    "goals_for": "GoalsFor",
    "goals_against": "GoalsAgainst",
}


def standing_values(standing: dict[str, Any]) -> dict[str, int | None]:
    """Keep absent/unusable values unknown, and preserve zero and penalties."""
    values: dict[str, int | None] = {}
    for name, source in STANDING_FIELDS.items():
        raw = standing.get(source)
        try:
            number = Decimal(str(raw))
            values[name] = (
                int(number)
                if number.is_finite()
                and abs(number) <= MAX_SAFE_INTEGER
                and number == number.to_integral_value()
                else None
            )
        except (InvalidOperation, ValueError, OverflowError):
            values[name] = None
    return values


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
