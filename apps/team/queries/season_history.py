"""Results, poule positions and Elo of club teams across every season.

The history pages compare seasons, so each read covers all of a team's
seasons at once. Results are aggregated in the database per team and season;
poule positions come from the official (or computed) standings of linked
competition poules, and the end-of-season Elo from the stored match ratings.
Fixtures are only read through their teams' indexed foreign keys, never by
scanning a season or the catalogue.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from operator import itemgetter
import re
from typing import Any
from uuid import UUID

from django.db.models import Count, F, IntegerField, OuterRef, Q, Subquery, Sum
from django.db.models.functions import Coalesce

from apps.competition.models import MatchRating, PoolEntry
from apps.game_tracker.models import MatchData
from apps.schedule.models import Season
from apps.schedule.queries.seasons import season_edition, season_kind
from apps.team.models.team import Team


Key = tuple[UUID, UUID]
RESULT_FIELDS = ("played", "won", "drawn", "lost", "goals_for", "goals_against")


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
    """Return the class and final position of each team's linked poules.

    Poules whose results are filtered to one club have no reliable table, so
    they keep their class but report no position.
    """
    pool_size = (
        PoolEntry.objects
        .filter(pool_id=OuterRef("pool_id"))
        .order_by()
        .values("pool_id")
        .annotate(size=Count("pk"))
        .values("size")
    )
    entries = (
        PoolEntry.objects
        .filter(
            team__group__local_team_id__in=list(team_ids),
            pool__local_pool__isnull=False,
        )
        .annotate(
            pool_size=Coalesce(Subquery(pool_size, output_field=IntegerField()), 0)
        )
        .values(
            "standing",
            "pool_size",
            local_team=F("team__group__local_team_id"),
            season=F("pool__local_pool__season_id"),
            local_pool=F("pool__local_pool_id"),
            pool_name=F("pool__name"),
            class_name=F("pool__class_name"),
            level=F("pool__competition_class__level"),
            results_filtered=F("pool__results_filtered"),
        )
        .order_by("pool__class_name", "pool__name", "pool_id")
    )
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
        position = str(entry["standing"].get("Position", ""))
        ranked = not entry["results_filtered"] and position.isdigit()
        poules[entry["local_team"], entry["season"]].append({
            "id": str(entry["local_pool"]),
            "name": entry["pool_name"],
            "class_name": entry["class_name"],
            "level": entry["level"],
            "position": int(position) if ranked else None,
            "teams": entry["pool_size"] if ranked else None,
        })
    return poules


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


def team_season_rows(team_ids: Iterable[UUID]) -> dict[UUID, list[dict[str, Any]]]:
    """Return each team's seasons, newest first, with results, poules and Elo."""
    ids = list(team_ids)
    results = _results(ids)
    poules = _poules(ids)
    ratings = _ratings(ids)
    keys = set(results) | set(poules)
    seasons = {
        season.pk: season
        for season in Season.objects.filter(pk__in={season for _, season in keys})
    }
    rows: dict[UUID, list[dict[str, Any]]] = defaultdict(list)
    for team, season_id in keys:
        season = seasons[season_id]
        rating = ratings.get((team, season_id))
        rows[team].append({
            "season": str(season.pk),
            "season_name": season.name,
            "start_date": season.start_date.isoformat(),
            "edition": season_edition(season),
            "discipline": season.discipline or None,
            "kind": season_kind(season),
            **results.get((team, season_id), dict.fromkeys(RESULT_FIELDS, 0)),
            "poules": poules.get((team, season_id), []),
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
    rows = team_season_rows(teams)
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
