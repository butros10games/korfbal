"""Resolve native playing seasons without changing provider fetch identities.

Both import paths route through these bindings. A binding maps a provider
scope and discipline, optionally narrowed to a competition period (phase), to
a native season. Historical editions bind each playing season to itself; live
scopes bind indoor play to an indoor season and, when explicitly configured,
independent outdoor halves to their own seasons while continuous outdoor
competitions stay in the annual scope.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date
from hashlib import sha256
import json
from uuid import UUID

from django.db import transaction
from django.db.models import Q

from apps.competition.domain.history_scopes import full_year_name, season_names
from apps.competition.models import Pool, SeasonBinding
from apps.schedule.domain.competition_context import (
    AUTUMN,
    FULL_SEASON,
    INDOOR_PHASE,
    SPRING,
)
from apps.schedule.models import Season
from apps.schedule.queries.seasons import season_edition
from apps.schedule.services.season_context import (
    edition_season,
    ensure_context,
    record_edition,
)


OUTDOOR = "KORFBALL-VE-WK"
INDOOR = "KORFBALL-ZA-WK"
OUTDOOR_PHASES = (AUTUMN, SPRING, FULL_SEASON)


def target_season(scope: Season, sport: str, phase: str = "") -> Season | None:
    """Unconfigured editions retain legacy behavior; configured unknowns fail closed."""
    bindings = {
        (row.sport, row.phase): row.season
        for row in SeasonBinding.objects.filter(scope=scope).select_related("season")
    }
    if not bindings:
        return scope
    if phase and (sport, phase) in bindings:
        return bindings[sport, phase]
    return bindings.get((sport, ""))


@transaction.atomic
def configure_seasons(
    scope: Season, year: int, *, split_outdoor: bool = False
) -> dict[str, Season]:
    """Create the native indoor season once for an explicitly identified edition.

    Dates describe a competition edition rather than inferred fixture boundaries.
    Existing outdoor dates and native season UUIDs remain authoritative. With
    ``split_outdoor`` the scope also routes independent autumn and spring
    poules to their own playing seasons; continuous poules stay in the scope.

    Returns:
        The default target per discipline.

    Raises:
        ValueError: A scope cannot silently change its established edition mapping.

    """
    if year != scope.start_date.year or season_edition(scope) not in {None, year}:
        raise ValueError("Edition year must agree with the configured import scope")
    Season.objects.select_for_update().get(pk=scope.pk)
    record_edition(scope, year)
    existing = list(SeasonBinding.objects.select_for_update().filter(scope=scope))
    defaults = {row.sport: row.season for row in existing if not row.phase}
    if existing and set(defaults) != {OUTDOOR, INDOOR}:
        raise ValueError("Incomplete season bindings require review")
    if not existing:
        indoor = edition_season(
            year,
            INDOOR_PHASE,
            f"Zaal seizoen {year}-{year + 1}",
            (date(year, 10, 1), date(year + 1, 6, 30)),
        )
        if (
            indoor.pk == scope.pk
            or indoor.start_date.year != year
            or indoor.end_date.year != year + 1
        ):
            raise ValueError(
                "Existing indoor season does not match the requested edition"
            )
        SeasonBinding.objects.bulk_create([
            SeasonBinding(scope=scope, sport=OUTDOOR, season=scope),
            SeasonBinding(scope=scope, sport=INDOOR, season=indoor),
        ])
        defaults = {OUTDOOR: scope, INDOOR: indoor}
    if split_outdoor:
        split_outdoor_phases(scope, year, existing)
    return defaults


def split_outdoor_phases(
    scope: Season, year: int, existing: list[SeasonBinding]
) -> dict[str, Season]:
    """Bind independent outdoor halves; the scope keeps continuous competitions.

    Returns:
        The target per outdoor phase.

    Raises:
        ValueError: An established phase binding points elsewhere.

    """
    ensure_context(scope, edition=year, phase=FULL_SEASON)
    targets = {
        AUTUMN: edition_season(
            year, AUTUMN, f"Voor seizoen {year}", (date(year, 7, 1), date(year, 12, 31))
        ),
        SPRING: edition_season(
            year,
            SPRING,
            f"Na seizoen {year + 1}",
            (date(year + 1, 1, 1), date(year + 1, 6, 30)),
        ),
        FULL_SEASON: scope,
    }
    current = {row.phase: row.season_id for row in existing if row.sport == OUTDOOR}
    for phase, season in targets.items():
        if phase in current and current[phase] != season.pk:
            raise ValueError(f"{scope.name} already routes {phase} elsewhere")
    SeasonBinding.objects.bulk_create(
        [
            SeasonBinding(scope=scope, sport=OUTDOOR, phase=phase, season=season)
            for phase, season in targets.items()
            if phase not in current
        ],
        ignore_conflicts=True,
    )
    return targets


def phase_split(scope_id: UUID, sport: str) -> bool:
    """Tell whether a scope routes this discipline by competition period."""
    return (
        SeasonBinding.objects
        .filter(scope_id=scope_id, sport=sport)
        .exclude(phase="")
        .exists()
    )


class SeasonResolver:
    """Load mappings once per publication pass, not once per entity."""

    def __init__(self, scopes: Iterable[UUID] | None = None) -> None:
        """Build the scope index in one query, optionally for selected scopes."""
        rows = SeasonBinding.objects.all()
        if scopes is not None:
            rows = rows.filter(scope_id__in=set(scopes))
        self.bindings = {
            (row.scope_id, row.sport, row.phase): row.season_id for row in rows
        }
        self.scopes = {scope for scope, _, _ in self.bindings}
        self.split = {(scope, sport) for scope, sport, phase in self.bindings if phase}

    def resolve(self, scope_id: UUID, sport: str, phase: str = "") -> UUID | None:
        """Return a target UUID or no decision for an unknown discipline."""
        if scope_id not in self.scopes:
            return scope_id
        if phase and (scope_id, sport, phase) in self.bindings:
            return self.bindings[scope_id, sport, phase]
        return self.bindings.get((scope_id, sport, ""))

    def splits(self, scope_id: UUID, sport: str) -> bool:
        """Tell whether publication must wait for a poule's competition period."""
        return (scope_id, sport) in self.split


def bound_scopes(season_id: UUID) -> list[UUID]:
    """Return the provider scopes whose rows can publish into a native season."""
    rows = list(SeasonBinding.objects.all().values_list("scope_id", "season_id"))
    bound = {scope for scope, _ in rows}
    scopes = {scope for scope, target in rows if target == season_id}
    if season_id not in bound:
        scopes.add(season_id)
    return sorted(scopes)


def native_season_filter(
    season_id: UUID,
    *,
    sport_field: str = "sport",
    scope_field: str = "season_id",
    phase_field: str | None = None,
) -> Q:
    """Filter source rows publishing into a native playing season.

    ``phase_field`` names the source row's competition period (a poule's
    ``phase``). Without it, phase-specific bindings cannot be distinguished and
    only the discipline's default binding applies.
    """
    bindings = list(SeasonBinding.objects.all())
    query = Q(**{scope_field: season_id}) & ~Q(**{
        scope_field + "__in": [row.scope_id for row in bindings]
    })
    specific: dict[tuple[UUID, str], list[str]] = {}
    for row in bindings:
        if row.phase:
            specific.setdefault((row.scope_id, row.sport), []).append(row.phase)
    for row in bindings:
        if row.season_id != season_id:
            continue
        rows = Q(**{scope_field: row.scope_id, sport_field: row.sport})
        if row.phase:
            if phase_field is None:
                continue
            rows &= Q(**{phase_field: row.phase})
        elif phase_field is not None and (row.scope_id, row.sport) in specific:
            rows &= ~Q(**{phase_field + "__in": specific[row.scope_id, row.sport]})
        query |= rows
    return query


def native_team_filter(season_id: UUID, *, prefix: str = "") -> Q:
    """Filter source teams with a roster participation in a native season."""
    return native_season_filter(
        season_id, sport_field=prefix + "sport", scope_field=prefix + "season_id"
    ) | Q(**{prefix + "participations__team_data__season_id": season_id})


def native_match_filter(season_id: UUID) -> Q:
    """Filter source fixtures belonging to a native season.

    A published fixture belongs where its native match is, whatever the
    bindings say now (a blocked repair or a pending period correction leaves it
    there); only unpublished fixtures follow the routing table.
    """
    return Q(local_match__season_id=season_id) | (
        Q(local_match__isnull=True)
        & native_season_filter(
            season_id, sport_field="home_team__sport", phase_field="pool__phase"
        )
    )


def native_pool_filter(season_id: UUID) -> Q:
    """Filter source poules belonging to a native season, published ones by link."""
    return Q(local_pool__season_id=season_id) | (
        Q(local_pool__isnull=True)
        & native_season_filter(season_id, phase_field="phase")
    )


def binding_fingerprint(season_id: UUID) -> str:
    """Digest the routing table and poule periods that decide a native population.

    Rebinding a scope or resolving a poule's period moves source rows between
    native seasons without changing any match row, so cached populations must
    include both. Only poules of scopes that can publish into the season count.
    """
    rows = sorted(
        (str(scope), sport, phase, str(season))
        for scope, sport, phase, season in SeasonBinding.objects.values_list(
            "scope_id", "sport", "phase", "season_id"
        )
    )
    periods = sorted(
        Pool.objects
        .filter(season_id__in=bound_scopes(season_id))
        .exclude(phase="")
        .values_list("pk", "phase", "competition_part")
    )
    payload = json.dumps([rows, periods], sort_keys=True, default=str)
    return sha256(payload.encode()).hexdigest()[:16]


def edition_scopes(edition: int) -> list[Season]:
    """Return the existing playing seasons of one edition."""
    query = Q(edition=edition, phase__in=(AUTUMN, INDOOR_PHASE, SPRING, FULL_SEASON))
    for name in (*season_names(edition), full_year_name(edition)):
        query |= Q(name__iexact=name)
    return list(Season.objects.filter(query))
