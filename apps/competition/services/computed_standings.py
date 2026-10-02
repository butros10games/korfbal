"""Standings computed from results, for poules without an official table.

Poules filled from a public result site (history_sites.py) have results but no
official standings. Their table is computed: two points for a win, one for a
draw. Checked against 2,600 official tables: points agree unless the official
table carries a deduction (3% of poules), and equal points are ordered by the
tied teams' mutual results, except in the youngest youth and midweek
competitions, where goal difference decides. That reproduces the official order
in about 97% of poules. A poule the provider delivers keeps its official
standings.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
import re
from typing import Any

from django.db import transaction
from django.utils import timezone

from apps.competition.models import Match, Pool, PoolEntry
from apps.competition.services.history import ARCHIVE_PREFIX
from apps.competition.services.history_editions import edition_scopes


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
                "Computed": True,
            },
        )
        for position, team in enumerate(ranked, start=1)
    ]


@transaction.atomic
def refresh_computed_standings(pool_ids: Iterable[int]) -> int:
    """Compute the table of every given poule that has only site results.

    A club team can have two source records in one poule (with and without a
    Sportlink code); it gets one row.

    Returns:
        The number of poules whose table was computed.

    """
    pool_ids = list(pool_ids)
    official = set(
        Match.objects
        .filter(pool_id__in=pool_ids)
        .exclude(external_id__startswith=ARCHIVE_PREFIX)
        .values_list("pool_id", flat=True)
    )
    refreshed = 0
    for pool in Pool.objects.select_for_update(no_key=True).filter(
        pk__in=set(pool_ids) - official
    ):
        entries: dict[Any, PoolEntry] = {}
        redundant = []
        for entry in (
            PoolEntry.objects
            .filter(pool=pool)
            .select_related("team")
            .order_by("team_id")
        ):
            identity = entry.team.group_id or f"team:{entry.team_id}"
            if identity in entries:
                redundant.append(entry.pk)
            else:
                entries[identity] = entry
        results = [
            (
                home_group or f"team:{home}",
                away_group or f"team:{away}",
                home_score,
                away_score,
            )
            for home, home_group, away, away_group, home_score, away_score in (
                Match.objects.filter(
                    pool=pool,
                    status="FINAL",
                    home_score__isnull=False,
                    away_score__isnull=False,
                ).values_list(
                    "home_team_id",
                    "home_team__group_id",
                    "away_team_id",
                    "away_team__group_id",
                    "home_score",
                    "away_score",
                )
            )
        ]
        if not results:
            continue
        changed = []
        mutual = GOAL_DIFFERENCE_CLASSES.match(pool.class_name) is None
        for identity, standing in standings(results, entries, mutual=mutual):
            entry = entries.get(identity)
            if entry is not None and entry.standing != standing:
                entry.standing = standing
                changed.append(entry)
        PoolEntry.objects.bulk_update(changed, ["standing"])
        PoolEntry.objects.filter(pk__in=redundant).delete()
        pool.results_filtered = False
        pool.standings_synced_at = timezone.now()
        pool.save(update_fields=("results_filtered", "standings_synced_at"))
        refreshed += 1
    return refreshed


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
