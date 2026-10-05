"""Bounded standings queries shared by catalogue read endpoints.

One authority decides a whole poule table (see domain/standings_provenance.py).
Each row carries whether its poule has official values anywhere, computed over
the whole poule before any page boundary, so a later page never switches to
generated values while the first page showed official ones.
"""

from collections.abc import Iterable
from decimal import Decimal, InvalidOperation
from typing import Any

from django.db import transaction
from django.db.models import (
    Case,
    F,
    IntegerField,
    Max,
    Q,
    QuerySet,
    Value,
    When,
    Window,
)
from django.db.models.fields.json import KeyTextTransform
from django.db.models.functions import Cast

from apps.competition.domain.standings_provenance import (
    COMPUTED,
    LEGACY_MARKER,
    OFFICIAL,
    content_digest,
    generated_standing,
    is_official_standing,
    table_digest,
    table_source,
)
from apps.competition.models import Pool, PoolEntry


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


POSITION = r"^[0-9]{1,9}$"
# A legacy generated row, written before generated tables had their own column.
LEGACY_ROW = Q(**{f"standing__{LEGACY_MARKER}": True})
OFFICIAL_ROW = ~Q(standing={}) & (
    ~LEGACY_ROW | Q(**{f"standing__{LEGACY_MARKER}__isnull": True})
)
GENERATED_ROW = Q(computed_standing__isnull=False) | LEGACY_ROW


def _official_row() -> Case:
    return Case(
        When(OFFICIAL_ROW, then=Value(1)), default=Value(0), output_field=IntegerField()
    )


def _position(field: str) -> Cast:
    return Cast(KeyTextTransform("Position", field), IntegerField())


def standing_entries() -> QuerySet[PoolEntry]:
    """Sort ranks numerically, official rows first, before any page boundary."""
    return (
        PoolEntry.objects
        .select_related("team__season", "team__group")
        .annotate(
            official_row=_official_row(),
            pool_official=Window(Max(_official_row()), partition_by=F("pool_id")),
            pool_generated=Window(
                Max(
                    Case(
                        When(GENERATED_ROW, then=Value(1)),
                        default=Value(0),
                        output_field=IntegerField(),
                    )
                ),
                partition_by=F("pool_id"),
            ),
            official_position=Case(
                When(
                    OFFICIAL_ROW & Q(standing__Position__regex=POSITION),
                    then=_position("standing"),
                ),
                default=None,
                output_field=IntegerField(),
            ),
            generated_position=Case(
                When(
                    computed_standing__Position__regex=POSITION,
                    then=_position("computed_standing"),
                ),
                When(
                    LEGACY_ROW & Q(standing__Position__regex=POSITION),
                    then=_position("standing"),
                ),
                default=None,
                output_field=IntegerField(),
            ),
        )
        .order_by(
            F("official_row").desc(),
            F("official_position").asc(nulls_last=True),
            F("generated_position").asc(nulls_last=True),
            "team_id",
        )
    )


def visible_standing_entries() -> QuerySet[PoolEntry]:
    """Apply visibility after whole-pool authority, including generated legacy flags."""
    return standing_entries().filter(
        Q(pool__results_filtered=False) | (Q(pool_official=0) & Q(pool_generated=1))
    )


def fallback_table_digests(pools: Iterable[Pool]) -> dict[int, dict[str, str]]:
    """Batch legacy content keys for selected pools without loading their teams."""
    candidates = {
        pool.pk: source
        for pool in pools
        if (source := pool_table_source(pool)) in {OFFICIAL, COMPUTED}
        and table_digest(source, pool.standings_provenance) is None
    }
    if not candidates:
        return {}
    values: dict[int, list[tuple[int, dict[str, Any] | None]]] = {
        pk: [] for pk in candidates
    }
    for pool_id, team_id, standing, computed in PoolEntry.objects.filter(
        pool_id__in=candidates
    ).values_list("pool_id", "team_id", "standing", "computed_standing"):
        current = standing if is_official_standing(standing) else {}
        values[pool_id].append((
            team_id,
            current
            if candidates[pool_id] == OFFICIAL
            else generated_standing(standing, computed),
        ))
    return {
        pk: {source: content_digest(values[pk])} for pk, source in candidates.items()
    }


def pool_table_source(pool: Pool) -> str:
    """Read whole-pool authority from the annotated first-page rows."""
    rows = pool.standing_rows
    return table_source(
        official=any(getattr(row, "pool_official", 0) for row in rows),
        generated=any(
            generated_standing(row.standing, row.computed_standing) is not None
            for row in rows
        ),
        results_filtered=pool.results_filtered,
    )


@transaction.atomic
def refresh_official_digests(pool_ids: Iterable[int]) -> None:
    """Invalidate reviewed official content after a changed source membership."""
    pools = list(
        Pool.objects
        .select_for_update(no_key=True)
        .filter(pk__in=set(pool_ids), standings_provenance__has_key="official_digest")
        .order_by("pk")
    )
    values: dict[int, list[tuple[int, dict[str, Any]]]] = {
        pool.pk: [] for pool in pools
    }
    for pool_id, team_id, standing in PoolEntry.objects.filter(
        pool_id__in=values
    ).values_list("pool_id", "team_id", "standing"):
        values[pool_id].append((
            team_id,
            standing if is_official_standing(standing) else {},
        ))
    for pool in pools:
        digest = content_digest(values[pool.pk])
        if pool.standings_provenance["official_digest"] == digest:
            continue
        provenance = dict(pool.standings_provenance)
        provenance.pop(OFFICIAL, None)
        provenance["official_digest"] = digest
        pool.standings_provenance = provenance
        pool.save(update_fields=("standings_provenance",))
