"""Resolve native playing seasons without changing provider fetch identities."""

from datetime import date
from uuid import UUID

from django.db import transaction
from django.db.models import Q

from apps.competition.models import SeasonBinding
from apps.schedule.models import Season


OUTDOOR = "KORFBALL-VE-WK"
INDOOR = "KORFBALL-ZA-WK"


def target_season(scope: Season, sport: str) -> Season | None:
    """Unconfigured editions retain legacy behavior; configured unknowns fail closed."""
    bindings = {
        row.sport: row.season
        for row in SeasonBinding.objects.filter(scope=scope).select_related("season")
    }
    return bindings.get(sport) if bindings else scope


@transaction.atomic
def configure_seasons(scope: Season, year: int) -> dict[str, Season]:
    """Create the native indoor season once for an explicitly identified edition.

    Dates describe a competition edition rather than inferred fixture boundaries.
    Existing outdoor dates and native season UUIDs remain authoritative.

    Raises:
        ValueError: A scope cannot silently change its established edition mapping.

    """
    if year != scope.start_date.year:
        raise ValueError("Edition year must agree with the configured import scope")
    Season.objects.select_for_update().get(pk=scope.pk)
    existing = list(SeasonBinding.objects.select_for_update().filter(scope=scope))
    if existing:
        if {row.sport for row in existing} != {OUTDOOR, INDOOR}:
            raise ValueError("Incomplete season bindings require review")
        return {row.sport: row.season for row in existing}
    indoor, _ = Season.objects.get_or_create(
        name=f"Zaal seizoen {year}-{year + 1}",
        defaults={"start_date": date(year, 10, 1), "end_date": date(year + 1, 6, 30)},
    )
    if (
        indoor.pk == scope.pk
        or indoor.start_date.year != year
        or indoor.end_date.year != year + 1
    ):
        raise ValueError("Existing indoor season does not match the requested edition")
    SeasonBinding.objects.bulk_create([
        SeasonBinding(scope=scope, sport=OUTDOOR, season=scope),
        SeasonBinding(scope=scope, sport=INDOOR, season=indoor),
    ])
    return {OUTDOOR: scope, INDOOR: indoor}


class SeasonResolver:
    """Load mappings once per publication pass, not once per entity."""

    def __init__(self) -> None:
        """Build the scope index in one query."""
        self.bindings = {
            (row.scope_id, row.sport): row.season_id
            for row in SeasonBinding.objects.all()
        }
        self.scopes = {scope for scope, _ in self.bindings}

    def resolve(self, scope_id: UUID, sport: str) -> UUID | None:
        """Return a target UUID or no decision for an unknown discipline."""
        return (
            self.bindings.get((scope_id, sport))
            if scope_id in self.scopes
            else scope_id
        )


def native_season_filter(
    season_id: UUID, *, sport_field: str = "sport", scope_field: str = "season_id"
) -> Q:
    """Filter a native playing season while preserving provider edition identities."""
    bindings = list(SeasonBinding.objects.all())
    query = Q(**{scope_field: season_id}) & ~Q(**{
        scope_field + "__in": [row.scope_id for row in bindings]
    })
    for row in bindings:
        if row.season_id == season_id:
            query |= Q(**{scope_field: row.scope_id, sport_field: row.sport})
    return query
