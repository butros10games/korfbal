"""Standings generated from results, for poules without an official table.

Poules filled from a public result site (history_sites.py) have results but no
official standings. Their table is computed: two points for a win, one for a
draw. Checked against 2,600 official tables: points agree unless the official
table carries a deduction (3% of poules), and equal points are ordered by the
tied teams' mutual results, except in the youngest youth and midweek
competitions, where goal difference decides. That reproduces the official order
in about 97% of poules, so a generated table is approximate and provisional.

A generated table is stored in ``PoolEntry.computed_standing`` with its
provenance on the poule, never in the official ``standing`` JSON: an official
table always wins, deductions stay unknown, and fixture coverage is never
claimed complete. Only canonical scored finals count; an archive copy of a
fixture the provider delivers itself is the same match.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.competition.domain.history_scopes import ARCHIVE_PREFIX
from apps.competition.domain.standings_provenance import (
    CALCULATION,
    COMPUTED,
    REASONS,
    computed_provenance,
    is_legacy_computed,
    is_official_standing,
)
from apps.competition.models import Match, Pool, PoolEntry
from apps.competition.services.seasons import edition_scopes


WIN_POINTS, DRAW_POINTS = 2, 1
COLUMNS = (
    "TotalMatches",
    "Won",
    "Draw",
    "Lost",
    "TotalPoints",
    "GoalsFor",
    "GoalsAgainst",
)
# Competitions whose official tables order equal points by goal difference.
GOAL_DIFFERENCE_CLASSES = re.compile(r"(?:[EF]\d*-jeugd|D4-jeugd|G-korfbal|Midweek)\b")
# One final result: home team, away team, home score, away score.
Result = tuple[Any, Any, int, int]
# Outcomes of writing one generated table.
WRITTEN, UNCHANGED, CLEARED = "written", "unchanged", "cleared"
OFFICIAL_PRESENT, NO_RESULTS = "official_present", "no_results"


def tally(results: Iterable[Result]) -> dict[Any, dict[str, int]]:
    """Add up played, won, drawn, lost, points and goals per team."""
    table: dict[Any, dict[str, int]] = defaultdict(lambda: dict.fromkeys(COLUMNS, 0))
    for home, away, home_score, away_score in results:
        for team, scored, conceded in (
            (home, home_score, away_score),
            (away, away_score, home_score),
        ):
            row = table[team]
            row["TotalMatches"] += 1
            row["GoalsFor"] += scored
            row["GoalsAgainst"] += conceded
            if scored > conceded:
                row["Won"] += 1
                row["TotalPoints"] += WIN_POINTS
            elif scored == conceded:
                row["Draw"] += 1
                row["TotalPoints"] += DRAW_POINTS
            else:
                row["Lost"] += 1
    return table


def difference(row: dict[str, int]) -> int:
    """Return a row's goal difference."""
    return row["GoalsFor"] - row["GoalsAgainst"]


def standings(
    results: list[Result], teams: Iterable[Any], *, mutual: bool = True
) -> list[tuple[Any, dict]]:
    """Rank teams by points, then mutual results (if used) and goal difference.

    Returns:
        Teams in table order with their standing, including teams without results.

    """
    table = tally(results)
    for team in teams:
        # A team without results still gets a row.
        table.setdefault(team, dict.fromkeys(COLUMNS, 0))
    by_points: dict[int, list[Any]] = defaultdict(list)
    for team, row in table.items():
        by_points[row["TotalPoints"]].append(team)
    ranked: list[Any] = []
    for points in sorted(by_points, reverse=True):
        tied = set(by_points[points])
        among = tally(
            result
            for result in results
            if mutual and result[0] in tied and result[1] in tied
        )
        ranked.extend(
            sorted(
                tied,
                key=lambda team, among=among: (
                    -among[team]["TotalPoints"] if team in among else 0,
                    -difference(among[team]) if team in among else 0,
                    -difference(table[team]),
                    -table[team]["GoalsFor"],
                    str(team),
                ),
            )
        )
    return [
        (
            team,
            {
                **table[team],
                "GoalsDifference": difference(table[team]),
                "Position": position,
            },
        )
        for position, team in enumerate(ranked, start=1)
    ]


def _identity(team_id: int, group_id: int | None) -> int | str:
    """Return one club team's identity across its source records."""
    return group_id or f"team:{team_id}"


@dataclass(frozen=True)
class Inputs:
    """A poule's canonical scored finals and whether any fixture lacks one."""

    results: list[Result]
    partial: bool


def canonical_results(pool_id: int) -> Inputs:
    """Read one poule's scored finals, counting each fixture once.

    An archive record with the same club teams and kickoff as a provider
    fixture is that fixture (see history_editions.supersede_archive); the
    provider's record decides, even when it is not final.
    """
    rows = list(
        Match.objects
        .filter(pool_id=pool_id)
        .order_by("pk")
        .values_list(
            "external_id",
            "status",
            "home_score",
            "away_score",
            "home_team_id",
            "home_team__group_id",
            "away_team_id",
            "away_team__group_id",
            "starts_at",
        )
    )
    provider = {
        (_identity(home, home_group), _identity(away, away_group), starts_at)
        for external_id, _, _, _, home, home_group, away, away_group, starts_at in rows
        if not external_id.startswith(ARCHIVE_PREFIX)
    }
    results: list[Result] = []
    partial = False
    for (
        external_id,
        status,
        home_score,
        away_score,
        home,
        home_group,
        away,
        away_group,
        starts_at,
    ) in rows:
        key = (_identity(home, home_group), _identity(away, away_group), starts_at)
        if external_id.startswith(ARCHIVE_PREFIX) and key in provider:
            continue
        if status != "FINAL" or home_score is None or away_score is None:
            partial = True
            continue
        results.append((key[0], key[1], home_score, away_score))
    return Inputs(results, partial)


def plan_table(
    pool: Pool, entries: list[PoolEntry], inputs: Inputs
) -> tuple[dict[int, dict | None], str]:
    """Return generated values per entry and a digest of the table's inputs.

    A club team can have two source records in one poule (with and without a
    Sportlink code); only the first gets a row.
    """
    canonical: dict[Any, PoolEntry] = {}
    for entry in sorted(entries, key=lambda entry: (entry.team_id, entry.pk)):
        canonical.setdefault(_identity(entry.team_id, entry.team.group_id), entry)
    mutual = GOAL_DIFFERENCE_CLASSES.match(pool.class_name) is None
    values: dict[int, dict | None] = {entry.pk: None for entry in entries}
    for identity, standing in standings(inputs.results, canonical, mutual=mutual):
        entry = canonical.get(identity)
        if entry is not None:
            values[entry.pk] = standing
    payload = {
        "calculation": CALCULATION,
        "mutual": mutual,
        "partial": inputs.partial,
        "results": sorted(
            json.dumps([str(value) for value in row]) for row in inputs.results
        ),
        "entries": sorted(
            [entry.pk, str(identity)] for identity, entry in canonical.items()
        ),
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[
        :32
    ]
    return values, digest


def locked_entries(pool: Pool) -> list[PoolEntry]:
    """Lock one poule's memberships without blocking foreign-key checks."""
    return list(
        PoolEntry.objects
        .select_for_update(no_key=True, of=("self",))
        .filter(pool=pool)
        .select_related("team")
        .order_by("pk")
    )


def write_generated_table(pool: Pool, entries: list[PoolEntry], *, reason: str) -> str:
    """Store a generated table beside, never over, official standings.

    The caller holds no-key locks on the poule and its ``entries``. Unchanged
    inputs write nothing, so reruns are idempotent.

    Returns:
        written, unchanged, cleared, official_present or no_results.

    """
    if any(is_official_standing(entry.standing) for entry in entries):
        return OFFICIAL_PRESENT
    inputs = canonical_results(pool.pk)
    provenance = dict(pool.standings_provenance or {})
    if not inputs.results:
        # Results were removed or superseded: the old table has no basis left.
        stale = [
            entry
            for entry in entries
            if entry.computed_standing is not None or is_legacy_computed(entry.standing)
        ]
        for entry in stale:
            entry.computed_standing = None
            if is_legacy_computed(entry.standing):
                entry.standing = {}
        PoolEntry.objects.bulk_update(stale, ["computed_standing", "standing"])
        if provenance.pop(COMPUTED, None) is None and not stale:
            return NO_RESULTS
        pool.standings_provenance = provenance
        pool.save(update_fields=("standings_provenance",))
        return CLEARED
    values, digest = plan_table(pool, entries, inputs)
    changed = [
        entry for entry in entries if entry.computed_standing != values[entry.pk]
    ]
    for entry in changed:
        entry.computed_standing = values[entry.pk]
    PoolEntry.objects.bulk_update(changed, ["computed_standing"])
    if not changed and (provenance.get(COMPUTED) or {}).get("digest") == digest:
        return UNCHANGED
    provenance[COMPUTED] = computed_provenance(
        reason=reason,
        results=len(inputs.results),
        partial=inputs.partial,
        digest=digest,
        computed_at=timezone.now().isoformat(),
    )
    pool.standings_provenance = provenance
    pool.save(update_fields=("standings_provenance",))
    return WRITTEN


@transaction.atomic
def refresh_computed_standings(pool_ids: Iterable[int]) -> int:
    """Compute the table of every given poule that has only site results.

    Returns:
        The number of poules that have a computed table.

    """
    pool_ids = set(pool_ids)
    official = set(
        Match.objects
        .filter(pool_id__in=pool_ids)
        .exclude(external_id__startswith=ARCHIVE_PREFIX)
        .values_list("pool_id", flat=True)
    )
    refreshed = 0
    for pool in (
        Pool.objects
        .select_for_update(no_key=True)
        .filter(pk__in=pool_ids - official)
        .order_by("pk")
    ):
        entries = locked_entries(pool)
        if write_generated_table(pool, entries, reason="archive_results") not in {
            WRITTEN,
            UNCHANGED,
        }:
            continue
        # Second source records of one club team leave membership unless an
        # allocation references them. Computing never changes official feed flags.
        redundant = [entry.pk for entry in entries if entry.computed_standing is None]
        PoolEntry.objects.filter(pk__in=redundant, allocations__isnull=True).delete()
        refreshed += 1
    return refreshed


@transaction.atomic
def refresh_generated_standings(pool_ids: Iterable[int]) -> dict[str, int]:
    """Recompute only touched poules that already hold a generated table.

    Call after scores, memberships or archive supersession change. Poules
    without a generated table (or with an official one) write nothing.

    Returns:
        Poules per outcome.

    """
    outcomes: Counter[str] = Counter()
    candidates = set(pool_ids)
    generated = (
        PoolEntry.objects
        .filter(pool_id__in=candidates)
        .filter(Q(computed_standing__isnull=False) | Q(standing__Computed=True))
        .values("pool_id")
    )
    for pool in (
        Pool.objects
        .select_for_update(no_key=True)
        .filter(pk__in=candidates)
        .filter(Q(standings_provenance__has_key=COMPUTED) | Q(pk__in=generated))
        .order_by("pk")
    ):
        reason = (pool.standings_provenance.get(COMPUTED) or {}).get("reason")
        outcomes[
            write_generated_table(
                pool,
                locked_entries(pool),
                reason=reason if reason in REASONS else "legacy_conversion",
            )
        ] += 1
    return dict(outcomes)


def refresh_edition_standings(edition: int, batch: int = 500) -> dict[str, int]:
    """Compute the tables of every site-filled poule of one edition.

    Returns:
        The edition and how many poules got a computed table.

    """
    pools = list(
        Pool.objects
        .filter(
            season__in=edition_scopes(edition),
            pk__in=Match.objects.filter(
                external_id__startswith=ARCHIVE_PREFIX, pool__isnull=False
            ).values("pool_id"),
        )
        .order_by("pk")
        .values_list("pk", flat=True)
    )
    # Short transactions beside the running import and publication.
    computed = sum(
        refresh_computed_standings(pools[start : start + batch])
        for start in range(0, len(pools), batch)
    )
    return {"edition": edition, "poules": computed}
