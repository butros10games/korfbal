"""Club.Dataservice history normalization with separate, explicit ID namespaces."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta
import re
from typing import Any
from zoneinfo import ZoneInfo

from django.db.models import Q, QuerySet
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.competition.models import (
    Club,
    HistoricalResource,
    Match,
    Pool,
    PoolEntry,
    Team,
)
from apps.competition.services.history import (
    DATA_ROW_LIMITS,
    HistoryUnavailableError,
    discover,
    reuse_pool_matches,
    seed,
    split_window,
    validate_match,
)
from apps.competition.services.importer import Importer


STANDINGS = {
    "positie": "Position",
    "gespeeldewedstrijden": "TotalMatches",
    "gewonnen": "Won",
    "gelijk": "Draw",
    "verloren": "Lost",
    "punten": "TotalPoints",
    "verliespunten": "PenaltyPoints",
    "doelpuntenvoor": "GoalsFor",
    "doelpuntentegen": "GoalsAgainst",
    "doelsaldo": "GoalsDifference",
}


def stamp(value: str) -> datetime:
    """Interpret documented Dutch local timestamps explicitly, never in server time.

    Raises:
        ValueError: The supplied configuration or response is inconsistent.

    """
    result = parse_datetime(value)
    if result is None:
        raise ValueError("Invalid Dataservice datetime")
    return (
        timezone.make_aware(result, ZoneInfo("Europe/Amsterdam"))
        if timezone.is_naive(result)
        else result.astimezone(ZoneInfo("Europe/Amsterdam"))
    )


def normalize(
    row: dict,
    resource: HistoricalResource,
    pool_id: str = "",
    *,
    clubs: dict[str, Club],
) -> dict:
    """Use club relation codes from the national catalogue, not guessed club names.

    Raises:
        HistoryUnavailableError: The historical scope cannot be established.

    """
    if not resource.sport:
        raise HistoryUnavailableError("sport_mapping_required")
    sides = {}
    for side in ("thuis", "uit"):
        club = clubs.get(row[f"{side}teamclubrelatiecode"])
        if club is None:
            raise HistoryUnavailableError("club_catalogue_required")
        sides[side] = {
            "PublicTeamId": f"ds:{row[f'{side}teamid']}",
            "TeamName": row[f"{side}team"],
            "SportId": resource.sport,
            "Club": {
                "ClubId": club.external_id,
                "ClubName": club.name,
                "City": club.city,
            },
        }
    score = re.fullmatch(
        r"\s*(\d+)\s*-\s*(\d+)\s*(?:\(\s*\d+\s*-\s*\d+\s*\))?\s*",
        str(row.get("uitslag", "")),
    )
    payload = {
        "PublicMatchId": f"ds:{row['wedstrijdcode']}",
        "MatchDateTime": stamp(row["wedstrijddatum"]).isoformat(),
        "Status": "FINAL" if score else "UNKNOWN",
        "AutoResult": None,
        "HomeTeam": sides["thuis"],
        "AwayTeam": sides["uit"],
        "HomeResult": {"Score": int(score[1]) if score else None},
        "AwayResult": {"Score": int(score[2]) if score else None},
    }
    if pool_id:
        payload["Pool"] = {"PoolId": f"ds:{pool_id}"}
    return payload


def apply_dataservice(resource: HistoricalResource, data: dict[str, Any]) -> None:
    """Dispatch one verified Dataservice response without speculative discovery."""
    handlers = {
        "pool": expand_pool,
        "window": apply_window,
        "pool_window": apply_window,
        "match": apply_match_details,
        "standing": apply_metadata,
        "members": apply_metadata,
    }
    handlers[resource.kind](resource, data)


def expand_pool(resource: HistoricalResource, data: dict[str, Any]) -> None:
    """Apply one historical expand pool operation."""
    importer = Importer(resource.season, timezone.now(), discover=False)
    importer.pool({"PoolId": f"ds:{resource.source_id}"}, resource.sport)
    for kind in ("standing", "members"):
        discover(resource, kind, resource.source_id)
    seed(
        resource.season,
        "dataservice",
        "pool_window",
        resource.source_id,
        start=resource.season.start_date,
        end=min(resource.season.end_date, timezone.localdate() - timedelta(days=1)),
        sport=resource.sport,
        parent=resource,
        reference=f"resource/{resource.pk}",
    )
    resource.coverage = "partial"


def select_window_rows(
    resource: HistoricalResource, data: dict[str, Any]
) -> list[dict]:
    """Validate response scope before a saturated interval can trigger more requests.

    Raises:
        HistoryUnavailableError: The historical scope cannot be established.

    """
    rows = data["rows"]
    dated = []
    for row in rows:
        day = stamp(row["wedstrijddatum"]).date()
        # Date filters must be verified from the response, including alignment padding.
        if not data["wire_start"] <= day <= resource.end_date:
            raise HistoryUnavailableError("date_filter_not_honored")
        dated.append((row, day))
    if resource.kind == "window" and any(
        resource.source_id
        not in {row["thuisteamclubrelatiecode"], row["uitteamclubrelatiecode"]}
        for row in rows
    ):
        raise HistoryUnavailableError("dataservice_club_scope_mismatch")
    if resource.kind == "pool_window" and any(
        row.get("poulecode") is not None and str(row["poulecode"]) != resource.source_id
        for row in rows
    ):
        raise HistoryUnavailableError("dataservice_pool_scope_mismatch")
    return [r for r, day in dated if resource.start_date <= day <= resource.end_date]


def apply_window(resource: HistoricalResource, data: dict[str, Any]) -> None:
    """Apply unique, consistent results from one verified historical window.

    Raises:
        HistoryUnavailableError: A result identity has conflicting provider values.

    """
    selected = select_window_rows(resource, data)
    if len(data["rows"]) >= DATA_ROW_LIMITS[resource.kind]:
        split_window(resource)
        return
    importer = Importer(resource.season, timezone.now(), discover=False)
    clubs = {
        club.external_id: club
        for club in Club.objects.filter(
            external_id__in={
                row[f"{side}teamclubrelatiecode"]
                for row in selected
                for side in ("thuis", "uit")
            }
        )
    }
    payloads = {}
    for row in selected:
        payload = normalize(
            row,
            resource,
            resource.source_id if resource.kind == "pool_window" else "",
            clubs=clubs,
        )
        validate_match(payload, resource)
        identifier = payload["PublicMatchId"]
        if identifier in payloads and payloads[identifier] != payload:
            raise HistoryUnavailableError("conflicting_result_identity")
        payloads[identifier] = payload
    pool_ids = set(
        Match.objects.filter(
            season=resource.season,
            external_id__in=payloads,
            pool__isnull=False,
        ).values_list("pool__external_id", flat=True)
    )
    if resource.kind == "pool_window" and payloads:
        pool_ids.add(f"ds:{resource.source_id}")
    for payload in payloads.values():
        importer.match(payload, result=True)
        if resource.kind == "window":
            discover(resource, "match", payload["PublicMatchId"].removeprefix("ds:"))
    if resource.kind == "pool_window":
        reuse_pool_matches(resource, list(payloads))
    resource.coverage = "partial" if selected else "empty"
    resource.evidence = {
        "rows": len(payloads),
        "interval_exhausted": True,
        "pool_ids": sorted(pool_ids),
    }
    return


def apply_match_details(resource: HistoricalResource, data: dict[str, Any]) -> None:
    """Apply one historical apply match details operation.

    Raises:
        HistoryUnavailableError: The historical scope cannot be established.

    """
    importer = Importer(resource.season, timezone.now(), discover=False)
    info = data["wedstrijdinformatie"]
    if (
        stamp(info["wedstrijddatetime"]).date() < resource.season.start_date
        or stamp(info["wedstrijddatetime"]).date() > resource.season.end_date
    ):
        raise HistoryUnavailableError("season_mismatch")
    match = (
        Match.objects
        .filter(season=resource.season, external_id=f"ds:{resource.source_id}")
        .select_related("home_team", "away_team", "pool")
        .first()
    )
    if match is None:
        raise HistoryUnavailableError("result_seed_required")
    if (
        stamp(info["wedstrijddatetime"]) != match.starts_at
        or str(info["thuisteamid"]) != match.home_team.external_id.removeprefix("ds:")
        or str(info["uitteamid"]) != match.away_team.external_id.removeprefix("ds:")
    ):
        raise HistoryUnavailableError("match_identity_mismatch")
    pool_ids = {match.pool.external_id} if match.pool is not None else set()
    if info.get("poulecode"):
        pool_id = str(info["poulecode"])
        pool = importer.pool(
            {
                "PoolId": f"ds:{pool_id}",
                "PoolName": info.get("poule", ""),
                "ClassName": info.get("klasse", ""),
            },
            resource.sport,
        )
        match.pool = pool
        match.save(update_fields=("pool",))
        discover(resource, "pool", pool_id)
        pool_ids.add(pool.external_id)
    resource.coverage = "partial"
    resource.evidence = {"pool_ids": sorted(pool_ids)}


def apply_metadata(resource: HistoricalResource, data: dict[str, Any]) -> None:
    """Apply one historical apply metadata operation.

    Raises:
        ValueError: The supplied configuration or response is inconsistent.

    """
    pool = Pool.objects.get(
        season=resource.season, external_id=f"ds:{resource.source_id}"
    )
    rows = data["rows"]
    if any(
        not isinstance(row.get(key), str)
        for row in rows
        for key in ("teamnaam", "clubrelatiecode")
    ):
        raise ValueError(
            "Dataservice standing and membership identities must be strings"
        )
    if resource.kind == "members":
        resource.evidence = {
            "members": [
                {k: row.get(k) for k in ("teamnaam", "clubrelatiecode")} for row in rows
            ]
        }
    elif resource.kind == "standing":
        resource.evidence = {
            "standings": [
                {
                    **{k: row.get(k) for k in ("teamnaam", "clubrelatiecode")},
                    **{key: row[key] for key in STANDINGS if key in row},
                }
                for row in rows
            ]
        }
    else:
        raise ValueError("Unknown Dataservice resource")
    resource.coverage = "partial" if rows else "empty"
    pool.results_filtered = True
    pool.save(update_fields=("results_filtered",))


def reconcile_standings(
    resource: HistoricalResource, pool: Pool, rows: list[dict]
) -> dict[int, Any]:
    """Resolve names in one query and write only changed, unambiguous standings."""
    identities = Counter(
        (row.get("teamnaam"), row.get("clubrelatiecode")) for row in rows
    )
    candidates = defaultdict(list)
    for identifier, name, club in Team.objects.filter(
        season_id=resource.season_id,
        external_id__startswith="ds:",
        sport=resource.sport,
        name__in={identity[0] for identity in identities},
        club__external_id__in={identity[1] for identity in identities},
    ).values_list("pk", "name", "club__external_id"):
        candidates[name, club].append(identifier)
    standings = {}
    for row in rows:
        identity = (row.get("teamnaam"), row.get("clubrelatiecode"))
        teams = candidates[identity]
        if identities[identity] != 1 or len(teams) != 1:
            continue
        standings[teams[0]] = {
            dest: row[src] for src, dest in STANDINGS.items() if src in row
        }
    existing = {entry.team_id: entry for entry in PoolEntry.objects.filter(pool=pool)}
    changed = []
    for team_id in existing.keys() | standings.keys():
        values = standings.get(team_id, {})
        if team_id in existing and existing[team_id].standing == values:
            continue
        changed.append(PoolEntry(pool=pool, team_id=team_id, standing=values))
    PoolEntry.objects.bulk_create(
        changed,
        update_conflicts=True,
        unique_fields=("pool", "team"),
        update_fields=("standing",),
    )
    return {
        identifier: values.get("TotalMatches")
        for identifier, values in standings.items()
    }


def intervals_exhausted(resource: HistoricalResource) -> bool:
    """Require an uninterrupted union of exhausted windows across the season."""
    through = min(resource.season.end_date, timezone.localdate() - timedelta(days=1))
    next_day = resource.season.start_date
    windows = HistoricalResource.objects.filter(
        provider="dataservice",
        kind="pool_window",
        season_id=resource.season_id,
        source_id=resource.source_id,
        state="fetched",
        evidence__interval_exhausted=True,
    ).order_by("start_date", "end_date")
    for start, end in windows.values_list("start_date", "end_date"):
        if start > next_day:
            return False
        next_day = max(next_day, end + timedelta(days=1))
        if next_day > through:
            return True
    return False


def reconcile_pool_coverage(resource_ids: set[int] | None = None) -> None:
    """Re-evaluate stored evidence without re-fetching complete historical feeds."""
    for resource in coverage_candidates(resource_ids):
        pool = Pool.objects.get(
            season=resource.season, external_id=f"ds:{resource.source_id}"
        )
        standing = HistoricalResource.objects.filter(
            provider="dataservice",
            kind="standing",
            season=resource.season,
            source_id=resource.source_id,
            state="fetched",
        ).first()
        if not standing:
            resource.coverage, resource.reason = "partial", "awaiting_standings"
            resource.save(update_fields=("coverage", "reason"))
            if not pool.results_filtered:
                pool.results_filtered = True
                pool.save(update_fields=("results_filtered",))
            continue
        rows = standing.evidence.get("standings", [])
        members = HistoricalResource.objects.filter(
            provider="dataservice",
            kind="members",
            season=resource.season,
            source_id=resource.source_id,
            state="fetched",
        ).first()
        membership = members.evidence.get("members", []) if members else []
        member_identities = [
            (row.get("teamnaam"), row.get("clubrelatiecode")) for row in membership
        ]
        members_agree = (
            bool(membership)
            and len(set(member_identities)) == len(member_identities)
            and Counter(member_identities)
            == Counter(
                (row.get("teamnaam"), row.get("clubrelatiecode")) for row in rows
            )
        )
        counts = reconcile_standings(resource, pool, rows)
        observed = {}
        matches = list(Match.objects.filter(pool=pool))
        for match in matches:
            if (
                match.status == "FINAL"
                and match.home_score is not None
                and match.away_score is not None
            ):
                for identifier in (match.home_team_id, match.away_team_id):
                    observed[identifier] = observed.get(identifier, 0) + 1
        exhausted = intervals_exhausted(resource)
        complete = (
            bool(rows)
            and len(counts) == len(rows)
            and bool(matches)
            and exhausted
            and members_agree
        )
        complete = (
            complete
            and set(observed) <= set(counts)
            and all(
                isinstance(v, int)
                and not isinstance(v, bool)
                and observed.get(k, 0) == v
                for k, v in counts.items()
            )
        )
        complete = complete and all(
            m.status == "FINAL"
            and m.home_score is not None
            and m.away_score is not None
            for m in matches
        )
        resource.coverage = "complete" if complete else "partial"
        resource.evidence = {
            "matches": len(matches),
            "standings_teams": len(rows),
            "resolved_teams": len(counts),
            "intervals_exhausted": exhausted,
            "members_agree": members_agree,
        }
        resource.reason = ""
        resource.save(update_fields=("coverage", "evidence", "reason"))
        if (
            pool.results_filtered != (not complete)
            or pool.standings_synced_at != standing.fetched_at
        ):
            pool.results_filtered = not complete
            pool.standings_synced_at = standing.fetched_at
            pool.save(update_fields=("results_filtered", "standings_synced_at"))


def affected_pools(resource_ids: set[int]) -> set[tuple]:
    """Limit coverage work to the graph nodes changed by this batch."""
    scope = set()
    match_ids = defaultdict(set)
    for resource in HistoricalResource.objects.filter(
        pk__in=resource_ids, provider="dataservice"
    ):
        if resource.kind in {"pool", "pool_window", "standing", "members"}:
            scope.add((resource.season_id, resource.source_id))
        scope.update(
            (resource.season_id, identifier.removeprefix("ds:"))
            for identifier in resource.evidence.get("pool_ids", [])
        )
        if resource.kind == "match":
            match_ids[resource.season_id].add(f"ds:{resource.source_id}")
    if match_ids:
        query = Q(pk__in=[])
        for season_id, identifiers in match_ids.items():
            query |= Q(season_id=season_id, external_id__in=identifiers)
        scope.update(
            (season_id, identifier.removeprefix("ds:"))
            for season_id, identifier in Match.objects.filter(
                query, pool__isnull=False
            ).values_list("season_id", "pool__external_id")
        )
    return scope


def coverage_candidates(resource_ids: set[int] | None) -> QuerySet[HistoricalResource]:
    """Select only changed poules, or every poule for an explicit audit."""
    pools = HistoricalResource.objects.filter(
        provider="dataservice", kind="pool", state="fetched"
    ).select_related("season")
    if resource_ids is not None:
        scope = affected_pools(resource_ids)
        if not scope:
            return pools.none()
        by_season = defaultdict(set)
        for season_id, pool_id in scope:
            by_season[season_id].add(pool_id)
        query = Q(pk__in=[])
        for season_id, pool_ids in by_season.items():
            query |= Q(season_id=season_id, source_id__in=pool_ids)
        pools = pools.filter(query)
    return pools
