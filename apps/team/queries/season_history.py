"""Results, poule positions and Elo of club teams across every season.

The history pages compare seasons, so each read covers all of a team's
seasons at once. Results are aggregated in the database per team and season;
poule positions come from the official (or generated) standings of linked
competition poules, and the end-of-season Elo from the stored match ratings.
Fixtures are only read through their teams' indexed foreign keys, never by
scanning a season or the catalogue.

A first place is not a championship: the legacy ``position`` field is only set
for a proven final official position (older apps label position 1 a champion),
while ``observed_position`` carries the shown table's rank with its provenance.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from operator import itemgetter
import re
from typing import Any
from uuid import UUID

from django.db.models import Count, F, Q, Sum

from apps.competition.domain.classification import Classification, ladder_context
from apps.competition.domain.standings_provenance import (
    COMPUTED,
    FINAL,
    OFFICIAL,
    fixture_coverage,
    generated_standing,
    is_official_standing,
    position,
    safe_integer,
    table_source,
    table_status,
    tied,
)
from apps.competition.models import MatchRating, PoolEntry
from apps.game_tracker.models import MatchData
from apps.schedule.models import Season
from apps.schedule.queries.seasons import season_edition, season_kind
from apps.team.models.team import Team


Key = tuple[UUID, UUID]
RESULT_FIELDS = ("played", "won", "drawn", "lost", "goals_for", "goals_against")
CLASS_FIELDS = (
    "code",
    "category",
    "age_group",
    "team_kind",
    "colour",
    "playing_format",
)
EDITION_FIELDS = ("discipline", "phase", "gender")


def _results(team_ids: Iterable[UUID]) -> dict[Key, dict[str, int]]:
    """Add up final scores per team and season, from both sides of a fixture."""
    ids = list(team_ids)
    totals: dict[Key, dict[str, int]] = defaultdict(
        lambda: dict.fromkeys(RESULT_FIELDS, 0)
    )
    for side, own, other in (
        ("home", "home_score", "away_score"),
        ("away", "away_score", "home_score"),
    ):
        rows = (
            MatchData.objects
            .filter(status="finished", **{f"match_link__{side}_team_id__in": ids})
            .order_by()
            .values(
                team=F(f"match_link__{side}_team_id"),
                season=F("match_link__season_id"),
            )
            .annotate(
                played=Count("pk"),
                won=Count("pk", filter=Q(**{f"{own}__gt": F(other)})),
                drawn=Count("pk", filter=Q(home_score=F("away_score"))),
                lost=Count("pk", filter=Q(**{f"{own}__lt": F(other)})),
                goals_for=Sum(own),
                goals_against=Sum(other),
            )
        )
        for row in rows:
            total = totals[row["team"], row["season"]]
            for field in RESULT_FIELDS:
                total[field] += row[field] or 0
    return totals


def _poules(team_ids: Iterable[UUID]) -> dict[Key, list[dict[str, Any]]]:
    """Return the class and observed table position of each team's linked poules.

    Each poule's whole table decides its authority (official or generated, never
    mixed) through one uncorrelated read of the selected poules' memberships.
    Poules whose results are filtered to one club have no reliable table, so
    they keep their class but report no position.
    """
    entries = list(
        PoolEntry.objects
        .filter(
            team__group__local_team_id__in=list(team_ids),
            pool__local_pool__isnull=False,
        )
        .values(
            "pool_id",
            "standing",
            "computed_standing",
            local_team=F("team__group__local_team_id"),
            season=F("pool__local_pool__season_id"),
            local_pool=F("pool__local_pool_id"),
            pool_name=F("pool__name"),
            class_name=F("pool__class_name"),
            level=F("pool__competition_class__level"),
            results_filtered=F("pool__results_filtered"),
            provenance=F("pool__standings_provenance"),
            competition_part=F("pool__competition_part"),
            mapping_issues=F("pool__mapping_issues"),
            class_id=F("pool__competition_class_id"),
            **{
                f"class_{field}": F(f"pool__competition_class__{field}")
                for field in CLASS_FIELDS
            },
            **{
                f"edition_{field}": F(f"pool__competition_class__edition__{field}")
                for field in EDITION_FIELDS
            },
        )
        .order_by("pool__class_name", "pool__name", "pool_id")
    )
    tables: dict[int, list[tuple[Any, Any]]] = defaultdict(list)
    for pool_id, standing, computed in PoolEntry.objects.filter(
        pool_id__in={entry["pool_id"] for entry in entries}
    ).values_list("pool_id", "standing", "computed_standing"):
        tables[pool_id].append((standing, computed))
    poules: dict[Key, list[dict[str, Any]]] = defaultdict(list)
    seen: set[tuple[UUID, UUID]] = set()
    for entry in entries:
        # Indoor and outdoor source entries of one team can share a poule.
        # Unmapped poules of a season that has not started carry no label yet.
        if (entry["local_team"], entry["local_pool"]) in seen or not (
            entry["pool_name"] or entry["class_name"]
        ):
            continue
        seen.add((entry["local_team"], entry["local_pool"]))
        poules[entry["local_team"], entry["season"]].append({
            "id": str(entry["local_pool"]),
            "name": entry["pool_name"],
            "class_name": entry["class_name"],
            "level": entry["level"],
            **_table_position(entry, tables[entry["pool_id"]]),
            "competition_part": entry["competition_part"],
            "class_code": entry["class_code"],
            "_class": entry,
        })
    return poules


def _table_position(entry: dict[str, Any], rows: list[tuple[Any, Any]]) -> dict:
    """Return one team's place in its poule's table with that table's provenance."""
    official = [standing for standing, _ in rows if is_official_standing(standing)]
    generated = [
        values
        for standing, computed in rows
        if (values := generated_standing(standing, computed)) is not None
    ]
    source = table_source(
        official=bool(official),
        generated=bool(generated),
        results_filtered=entry["results_filtered"],
    )
    own: dict[str, Any] | None = None
    table: list[Any] = []
    if source == OFFICIAL and is_official_standing(entry["standing"]):
        own, table = entry["standing"], official
    elif source == COMPUTED:
        own = generated_standing(entry["standing"], entry["computed_standing"])
        table = generated
    status = table_status(source, entry["provenance"])
    observed = position(own)
    return {
        # Older apps call position 1 a champion: only a proven final official
        # position may appear here.
        "position": observed if source == OFFICIAL and status == FINAL else None,
        "observed_position": observed,
        "teams": len(table) if observed is not None else None,
        "table_source": source,
        "table_status": status,
        "fixture_coverage": fixture_coverage(source, entry["provenance"]),
        # Official points already include the deduction; never subtract it again.
        "penalty_points": (
            safe_integer(own.get("PenaltyPoints"))
            if own is not None and source == OFFICIAL
            else None
        ),
        "tied": own is not None and tied(source, own, table),
        # Only a sourced title rule may claim a championship; none is modelled.
        "is_champion": False,
    }


def _ladder(entry: dict[str, Any], edition: int | None) -> dict[str, Any]:
    """Return the class's ladder lane for comparisons, as the poule API does."""
    if entry["class_id"] is None:
        return {"ladder_id": None, "hierarchy_revision": None, "level_reason": None}
    value = Classification(
        **{field: entry[f"class_{field}"] for field in CLASS_FIELDS},
        **{field: entry[f"edition_{field}"] for field in EDITION_FIELDS},
    )
    context = ladder_context(value, list(entry["mapping_issues"] or []), edition)
    return {
        field: context[field]
        for field in ("ladder_id", "hierarchy_revision", "level_reason")
    }


def _poule_seasons(team_ids: Iterable[UUID]) -> set[Key]:
    """Return the seasons in which each team has a labelled linked poule."""
    return set(
        PoolEntry.objects
        .filter(
            team__group__local_team_id__in=list(team_ids),
            pool__local_pool__isnull=False,
        )
        .exclude(pool__name="", pool__class_name="")
        .values_list("team__group__local_team_id", "pool__local_pool__season_id")
        .distinct()
    )


def _ratings(team_ids: Iterable[UUID]) -> dict[Key, dict[str, float]]:
    """Return each team's Elo before its first and after its last rated match."""
    ids = list(team_ids)
    seasons: dict[Key, dict[str, float]] = {}
    for side in ("home", "away"):
        rows = (
            MatchRating.objects
            .filter(**{f"{side}_team_id__in": ids}, phase_id__isnull=False)
            .order_by("starts_at", "match_id")
            .values_list(
                f"{side}_team_id",
                "phase_id",
                "starts_at",
                f"{side}_rating",
                "home_change",
            )
        )
        for team, phase, starts_at, before, home_change in rows:
            after = before + (home_change if side == "home" else -home_change)
            current = seasons.get((team, phase))
            if current is None:
                seasons[team, phase] = {
                    "first": starts_at.timestamp(),
                    "start": before,
                    "last": starts_at.timestamp(),
                    "end": after,
                }
                continue
            if starts_at.timestamp() < current["first"]:
                current["first"], current["start"] = starts_at.timestamp(), before
            if starts_at.timestamp() >= current["last"]:
                current["last"], current["end"] = starts_at.timestamp(), after
    return seasons


def team_season_rows(
    team_ids: Iterable[UUID], *, with_poules: bool = True
) -> dict[UUID, list[dict[str, Any]]]:
    """Return each team's seasons, newest first, with results, poules and Elo.

    Summaries that only count seasons skip the poule tables (``with_poules``).
    """
    ids = list(team_ids)
    results = _results(ids)
    poules = _poules(ids) if with_poules else {}
    ratings = _ratings(ids)
    keys = set(results) | (set(poules) if with_poules else _poule_seasons(ids))
    seasons = {
        season.pk: season
        for season in Season.objects.filter(pk__in={season for _, season in keys})
    }
    rows: dict[UUID, list[dict[str, Any]]] = defaultdict(list)
    for team, season_id in keys:
        season = seasons[season_id]
        rating = ratings.get((team, season_id))
        edition = season_edition(season)
        rows[team].append({
            "season": str(season.pk),
            "season_name": season.name,
            "start_date": season.start_date.isoformat(),
            "edition": edition,
            "discipline": season.discipline or None,
            "phase": season.phase or None,
            "kind": season_kind(season),
            **results.get((team, season_id), dict.fromkeys(RESULT_FIELDS, 0)),
            "poules": [
                {
                    **{key: value for key, value in poule.items() if key != "_class"},
                    **_ladder(poule["_class"], edition),
                }
                for poule in poules.get((team, season_id), [])
            ],
            "rating": round(rating["end"], 1) if rating else None,
            "rating_change": (
                round(rating["end"] - rating["start"], 1) if rating else None
            ),
        })
    for team_rows in rows.values():
        team_rows.sort(key=itemgetter("start_date", "season"), reverse=True)
    return rows


def team_season_history(team: Team) -> dict[str, Any]:
    """Return one team's seasons for its history section."""
    return {"seasons": team_season_rows([team.pk]).get(team.pk, [])}


def _natural(name: str) -> list[tuple[int, int | str]]:
    """Order "2" before "10" and "A1" before "A2"."""
    return [
        (0, int(part)) if part.isdigit() else (1, part.casefold())
        for part in re.split(r"(\d+)", name)
        if part
    ]


def _edition_key(row: dict[str, Any]) -> str:
    edition = row["edition"]
    return str(edition) if edition is not None else f"season:{row['season']}"


def club_season_history(club_id: UUID) -> dict[str, Any]:
    """Return the club's totals per korfbal year and the teams with history.

    A korfbal year runs from July to June. Seasons without a resolved year are
    listed on their own. Team seasons are read per team, so the club summary
    stays small for clubs with many teams over many years.
    """
    teams = {
        team.pk: team
        for team in Team.objects.filter(club_id=club_id).only("id_uuid", "name")
    }
    rows = team_season_rows(teams, with_poules=False)
    editions: dict[str, dict[str, Any]] = {}
    for team_id, team_rows in rows.items():
        for row in team_rows:
            key = _edition_key(row)
            edition = editions.setdefault(
                key,
                {
                    "key": key,
                    "edition": row["edition"],
                    "season_name": None
                    if row["edition"] is not None
                    else row["season_name"],
                    "start_date": row["start_date"],
                    "teams": set(),
                    **dict.fromkeys(RESULT_FIELDS, 0),
                },
            )
            edition["start_date"] = min(edition["start_date"], row["start_date"])
            edition["teams"].add(team_id)
            for field in RESULT_FIELDS:
                edition[field] += row[field]
    # Teams of the latest season first, each group in natural name order.
    ordered = sorted(rows, key=lambda team_id: _natural(teams[team_id].name))
    ordered.sort(key=lambda team_id: rows[team_id][0]["start_date"], reverse=True)
    team_summaries = [
        {
            "id": str(team_id),
            "name": teams[team_id].name,
            "seasons": len(rows[team_id]),
            "first_season": rows[team_id][-1]["season_name"],
            "last_season": rows[team_id][0]["season_name"],
            "played": sum(row["played"] for row in rows[team_id]),
        }
        for team_id in ordered
    ]
    return {
        "editions": sorted(
            (
                {**edition, "teams": len(edition["teams"])}
                for edition in editions.values()
            ),
            key=itemgetter("start_date", "key"),
            reverse=True,
        ),
        "teams": team_summaries,
    }
