"""Season-scoped KNKV app history: three playing seasons and a per-match log.

The app's ``SeasonId`` (the edition's start year) selects a past edition on
``TeamCompetitionData`` and ``PoolCompetitionData``. One edition contains three
playing seasons: outdoor summer/autumn, indoor, and outdoor spring/summer of the
next calendar year. Outdoor poules can run in either half or span both, so every
result row is routed to its playing season by sport and date.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import date
import re
from typing import Any

from django.db import transaction
from django.db.models import Count, Q, QuerySet
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.competition.models import (
    HistoricalDiscovery,
    HistoricalResource,
    Match,
    SeasonBinding,
    Team,
)
from apps.competition.services.history import (
    EDITION_KINDS,
    reference_label,
    resource_key,
    validate_identity,
)
from apps.competition.services.importer import Importer
from apps.competition.services.seasons import INDOOR, OUTDOOR
from apps.schedule.models import Season


# Catalogue teams probed per stratum before the rest of the stratum is queued.
PROBE_SIZE = 40
# Poule IDs scanned past the outermost poule with data. Observed edition ranges
# hold data for 85-98% of IDs, so a gap this long ends the edition.
SCAN_MARGIN = 200
# Outdoor matches from July onwards belong to the summer/autumn half.
AUTUMN_FIRST_MONTH = 7
# The oldest edition the app's season list offers.
FIRST_EDITION = 2006
SPORTS = (OUTDOOR, INDOOR)


@dataclass(frozen=True)
class EditionSeasons:
    """The three native playing seasons of one provider edition."""

    edition: int
    autumn: Season
    indoor: Season
    spring: Season

    def target(self, sport: str, day: date) -> Season | None:
        """Route a match to its playing season; other disciplines are unsupported."""
        if sport == INDOOR:
            return self.indoor
        if sport == OUTDOOR:
            return self.autumn if day.month >= AUTUMN_FIRST_MONTH else self.spring
        return None


def season_names(edition: int) -> tuple[str, str, str]:
    """Name the autumn outdoor, indoor and spring outdoor seasons of an edition."""
    return (
        f"Voor seizoen {edition}",
        f"Zaal seizoen {edition}-{edition + 1}",
        f"Na seizoen {edition + 1}",
    )


def current_edition() -> int:
    """Editions start in July; the running edition is not historical."""
    today = timezone.localdate()
    return today.year if today.month >= AUTUMN_FIRST_MONTH else today.year - 1


def _named_season(name: str, defaults: tuple[date, date] | None) -> Season:
    """Reuse an existing season by name, creating it only when allowed.

    Raises:
        ValueError: The name is ambiguous or the season does not exist.

    """
    matches = list(Season.objects.filter(name__iexact=name))
    if len(matches) > 1:
        raise ValueError(f"More than one season is named {name!r}")
    if matches:
        return matches[0]
    if defaults is None:
        raise ValueError(f"Season {name!r} does not exist; seed the edition first")
    return Season.objects.create(
        name=name, start_date=defaults[0], end_date=defaults[1]
    )


def _bind(scope: Season, sport: str) -> None:
    """Bind a playing season to itself for one sport, keeping existing bindings.

    Raises:
        ValueError: The season already sends this sport to another season.

    """
    binding, _ = SeasonBinding.objects.get_or_create(
        scope=scope, sport=sport, defaults={"season": scope}
    )
    if binding.season_id != scope.pk:
        raise ValueError(f"{scope.name} already sends {sport} to another season")


@transaction.atomic
def prepare_edition(edition: int) -> EditionSeasons:
    """Create or reuse the edition's playing seasons and their sport bindings.

    Raises:
        ValueError: The edition is not finished or its seasons are inconsistent.

    """
    if not FIRST_EDITION <= edition < current_edition():
        raise ValueError("Only finished editions can be imported")
    autumn_name, indoor_name, spring_name = season_names(edition)
    seasons = EditionSeasons(
        edition,
        autumn=_named_season(autumn_name, (date(edition, 7, 1), date(edition, 12, 31))),
        indoor=_named_season(
            indoor_name, (date(edition, 10, 1), date(edition + 1, 6, 30))
        ),
        spring=_named_season(
            spring_name, (date(edition + 1, 1, 1), date(edition + 1, 6, 30))
        ),
    )
    # The indoor season anchors discovery checkpoints; its start year is the
    # provider SeasonId.
    if seasons.indoor.start_date.year != edition:
        raise ValueError(f"{indoor_name} must start in {edition}")
    _bind(seasons.autumn, OUTDOOR)
    _bind(seasons.indoor, INDOOR)
    _bind(seasons.spring, OUTDOOR)
    return seasons


def edition_seasons(resource: HistoricalResource) -> EditionSeasons:
    """Load the playing seasons of an edition checkpoint's anchor season."""
    edition = resource.season.start_date.year
    autumn, indoor, spring = season_names(edition)
    return EditionSeasons(
        edition,
        autumn=_named_season(autumn, None),
        indoor=_named_season(indoor, None),
        spring=_named_season(spring, None),
    )


def edition_scopes(edition: int) -> list[Season]:
    """Return the existing playing seasons of one edition."""
    query = Q()
    for name in season_names(edition):
        query |= Q(name__iexact=name)
    return list(Season.objects.filter(query))


def seed_many(
    anchor: Season,
    kind: str,
    items: dict[str, str],
    *,
    parent: HistoricalResource | None,
    reference: str,
) -> int:
    """Queue many edition checkpoints in bulk; completed work is never reset.

    Returns:
        The number of newly queued checkpoints.

    """
    start, end = anchor.start_date, anchor.end_date
    keys = {}
    for source_id, sport in items.items():
        validate_identity("app", kind, source_id)
        keys[resource_key(anchor, "app", kind, source_id, (start, end))] = (
            source_id,
            sport,
        )
    existing = set(
        HistoricalResource.objects.filter(key__in=keys).values_list("key", flat=True)
    )
    HistoricalResource.objects.bulk_create(
        [
            HistoricalResource(
                key=key,
                season=anchor,
                provider="app",
                kind=kind,
                source_id=source_id,
                start_date=start,
                end_date=end,
                sport=sport,
            )
            for key, (source_id, sport) in keys.items()
            if key not in existing
        ],
        ignore_conflicts=True,
        batch_size=1000,
    )
    label = reference_label(reference)
    HistoricalDiscovery.objects.bulk_create(
        [
            HistoricalDiscovery(resource_id=pk, parent=parent, reference=label)
            for pk in HistoricalResource.objects.filter(key__in=keys).values_list(
                "pk", flat=True
            )
        ],
        ignore_conflicts=True,
        batch_size=1000,
    )
    return len(keys.keys() - existing)


def team_stratum(name: str, sport: str) -> str:
    """Group catalogue teams by discipline and age; youth IDs change every season."""
    token = name.rsplit(maxsplit=1)[-1] if name.strip() else ""
    youth = re.fullmatch(r"U\d{1,2}(-\d+)?|J\d+|[A-F]\d+|W\d+", token)
    return f"{sport}/{'youth' if youth else 'senior'}"


def catalogue_strata() -> dict[str, dict[str, str]]:
    """Return catalogue team IDs and sports per stratum, in a stable order."""
    strata: dict[str, dict[str, str]] = defaultdict(dict)
    seen: set[str] = set()
    for source_id, name, sport in (
        Team.objects
        .filter(sport__in=SPORTS, external_id__regex=r"^T[0-9]+$")
        .order_by("external_id")
        .values_list("external_id", "name", "sport")
    ):
        if source_id not in seen:
            seen.add(source_id)
            strata[team_stratum(name, sport)][source_id] = sport
    return dict(strata)


def probe_label(edition: int, stratum: str) -> str:
    """Label the provenance of a stratum's probe seeds."""
    return f"catalogue-probe/{edition}/{stratum}"


def release_label(edition: int, stratum: str) -> str:
    """Label a stratum's remaining seeds, queued once a probe finds data."""
    return f"catalogue/{edition}/{stratum}"


def seed_edition(
    edition: int, *, scan: bool = True, all_seeds: bool = False
) -> dict[str, Any]:
    """Queue probe teams for an edition and choose how poules are discovered.

    ``scan`` (the default) reads the edition's poule numbers directly: poule IDs
    of one edition are dense, and every poule response already contains all of
    its matches and teams, including youth teams that current IDs never reach.
    Probe teams only locate the first poules. Without ``scan``, a probe with
    data queues the rest of its catalogue stratum and teams found in old poules
    are queued in turn. Most current teams did not exist in an old edition (youth
    team IDs change every season), so each stratum first spends at most
    ``PROBE_SIZE`` requests.
    """
    seasons = prepare_edition(edition)
    if scan:
        start_scan(seasons.indoor, edition)
    queued: dict[str, int] = {}
    for stratum, teams in catalogue_strata().items():
        if scan and not stratum.endswith("/senior"):
            continue
        probe = dict(list(teams.items())[:PROBE_SIZE])
        queued[stratum] = seed_many(
            seasons.indoor,
            "edition_team",
            probe,
            parent=None,
            reference=probe_label(edition, stratum),
        )
        if all_seeds and not scan:
            queued[stratum] += seed_many(
                seasons.indoor,
                "edition_team",
                teams,
                parent=None,
                reference=release_label(edition, stratum),
            )
    return {
        "edition": edition,
        "mode": "scan" if scan else "teams",
        "seasons": [s.name for s in (seasons.autumn, seasons.indoor, seasons.spring)],
        "teams_queued": queued,
    }


def start_scan(anchor: Season, edition: int) -> None:
    """Mark an edition for poule scanning; the marker also keeps the window."""
    start, end = anchor.start_date, anchor.end_date
    source_id = str(edition)
    HistoricalResource.objects.get_or_create(
        key=resource_key(anchor, "app", "edition_scan", source_id, (start, end)),
        defaults={
            "season": anchor,
            "provider": "app",
            "kind": "edition_scan",
            "source_id": source_id,
            "start_date": start,
            "end_date": end,
            # Never fetched: the worker only picks pending checkpoints.
            "state": "fetched",
            "coverage": "partial",
            "fetched_at": timezone.now(),
        },
    )


def scan_marker(anchor: Season) -> HistoricalResource | None:
    """Return an edition's locked scan marker, or None in team mode."""
    return (
        HistoricalResource.objects
        .select_for_update()
        .filter(season=anchor, provider="app", kind="edition_scan")
        .first()
    )


def extend_scan(marker: HistoricalResource, resource: HistoricalResource) -> int:
    """Widen the contiguous scan window to this poule plus ``SCAN_MARGIN``.

    Gaps between found poules are filled, and scanning stops ``SCAN_MARGIN``
    IDs after the last poule with data in either direction.

    Returns:
        The number of newly queued poule IDs.

    """
    found = int(resource.source_id)
    low, high = marker.evidence.get("low"), marker.evidence.get("high")
    new_low = max(1, found - SCAN_MARGIN)
    new_high = found + SCAN_MARGIN
    if low is None or high is None:
        ids = range(new_low, new_high + 1)
    else:
        new_low, new_high = min(low, new_low), max(high, new_high)
        ids = [*range(new_low, low), *range(high + 1, new_high + 1)]
    marker.evidence = {"low": new_low, "high": new_high}
    marker.save(update_fields=("evidence",))
    return seed_many(
        resource.season,
        "edition_pool",
        dict.fromkeys(map(str, ids), ""),
        parent=resource,
        reference=f"scan/{marker.source_id}",
    )


def release_stratum(resource: HistoricalResource, edition: int) -> None:
    """Queue a stratum's remaining catalogue teams after its probe finds data."""
    probe = (
        HistoricalDiscovery.objects
        .filter(resource=resource, reference__startswith=f"catalogue-probe/{edition}/")
        .values_list("reference", flat=True)
        .first()
    )
    if probe is None:
        return
    stratum = probe.removeprefix(f"catalogue-probe/{edition}/")
    label = release_label(edition, stratum)
    if HistoricalDiscovery.objects.filter(
        resource__season=resource.season, reference=label
    ).exists():
        return
    seed_many(
        resource.season,
        "edition_team",
        catalogue_strata().get(stratum, {}),
        parent=resource,
        reference=label,
    )


def apply_edition(resource: HistoricalResource, data: dict[str, Any]) -> None:
    """Import one season-scoped team or poule response and log every match.

    Raises:
        ValueError: The response is an error envelope.

    """
    if data.get("Error"):
        raise ValueError("Sportlink returned an application error")
    seasons = edition_seasons(resource)
    marker = scan_marker(resource.season)
    if resource.kind == "edition_team":
        apply_team(resource, data, seasons, scan=marker is not None)
    else:
        apply_pool(resource, data, seasons, marker)


def apply_team(
    resource: HistoricalResource,
    data: dict[str, Any],
    seasons: EditionSeasons,
    *,
    scan: bool,
) -> None:
    """Queue a team's poules, including play-off poules missing from Pool[]."""
    unbound = (data.get("UnboundMatchResults") or {}).get("MatchResult") or []
    pools = {
        str(row["PoolId"]): resource.sport
        for row in [
            *(data.get("Pool") or []),
            *(row["Pool"] for row in unbound if row.get("Pool")),
        ]
    }
    seed_many(
        resource.season,
        "edition_pool",
        pools,
        parent=resource,
        reference=f"resource/{resource.pk}",
    )
    rows = [row for row in unbound if not row.get("Pool")]
    summary = import_rows(seasons, resource, rows)
    found = bool(pools or rows)
    if found and not scan:
        release_stratum(resource, seasons.edition)
    resource.coverage = "complete" if found else "empty"
    resource.reason = "" if found else "no_season_data"
    resource.evidence = {"edition": seasons.edition, "pools": sorted(pools)}
    resource.evidence.update(summary)


def apply_pool(
    resource: HistoricalResource,
    data: dict[str, Any],
    seasons: EditionSeasons,
    marker: HistoricalResource | None,
) -> None:
    """Import a poule's results and standings, then widen discovery.

    Raises:
        ValueError: A result names another poule.

    """
    rows = data["MatchResult"]
    for row in rows:
        if row.get("Pool") and str(row["Pool"]["PoolId"]) != resource.source_id:
            raise ValueError("Unexpected poule identity")
    if not rows:
        resource.coverage, resource.reason = "empty", "no_season_data"
        resource.evidence = {"edition": seasons.edition, "matches": 0}
        return
    standing = (data.get("PoolStanding") or {}).get("PoolStandingTeam") or []
    filtered = data.get("ResultsFiltered") is not False
    summary = import_rows(
        seasons, resource, rows, standing=data.get("PoolStanding"), filtered=filtered
    )
    if marker is not None:
        summary["scan_queued"] = extend_scan(marker, resource)
    else:
        # Teams that no longer exist are only reachable through old poules.
        teams = {
            str(team["PublicTeamId"]): team.get("SportId") or ""
            for team in [
                *standing,
                *(row[side] for row in rows for side in ("HomeTeam", "AwayTeam")),
            ]
            if team.get("SportId") in SPORTS
        }
        seed_many(
            resource.season,
            "edition_team",
            teams,
            parent=resource,
            reference=f"resource/{resource.pk}",
        )
    complete, played = pool_complete(rows, standing, filtered=filtered)
    complete = complete and not summary["skipped"]
    resource.coverage = "complete" if complete else "partial"
    resource.reason = "" if complete else "standings_results_disagree"
    resource.evidence = {"edition": seasons.edition, **summary, **played}


def pool_complete(
    rows: list[dict], standing: list[dict], *, filtered: bool
) -> tuple[bool, dict[str, dict]]:
    """Require each team's official played count to equal its scored finals."""
    observed: Counter[str] = Counter()
    for row in rows:
        if final_score(row) is not None:
            for side in ("HomeTeam", "AwayTeam"):
                observed[str(row[side]["PublicTeamId"])] += 1
    expected = {str(row["PublicTeamId"]): row.get("TotalMatches") for row in standing}
    complete = (
        not filtered
        and bool(expected)
        and set(observed) <= set(expected)
        and all(observed[team] == total for team, total in expected.items())
    )
    return complete, {"expected_played": expected, "observed_played": dict(observed)}


def final_score(row: dict[str, Any]) -> tuple[int, int] | None:
    """Return the final score, or None for unplayed or unscored rows."""
    home = (row.get("HomeResult") or {}).get("Score")
    away = (row.get("AwayResult") or {}).get("Score")
    if row.get("Status") != "FINAL" or home is None or away is None:
        return None
    return home, away


def route(
    seasons: EditionSeasons, row: dict[str, Any]
) -> tuple[Season | None, date | None, str]:
    """Choose the playing season for one result row, or the reason to skip it."""
    stamp = parse_datetime(str(row.get("MatchDateTime") or ""))
    if stamp is None or timezone.is_naive(stamp):
        return None, None, "invalid_timestamp"
    day = timezone.localdate(stamp)
    if day >= timezone.localdate():
        return None, day, "not_historical"
    sports = {row[side].get("SportId") for side in ("HomeTeam", "AwayTeam")}
    if len(sports) != 1:
        return None, day, "sport_mismatch"
    target = seasons.target(sports.pop() or "", day)
    if target is None:
        return None, day, "unsupported_sport"
    if not target.start_date <= day <= target.end_date:
        return None, day, "outside_season_dates"
    return target, day, ""


def import_rows(
    seasons: EditionSeasons,
    resource: HistoricalResource,
    rows: list[dict[str, Any]],
    *,
    standing: dict | None = None,
    filtered: bool = True,
) -> dict[str, Any]:
    """Import rows into their playing seasons, with standings on the final one.

    Returns:
        Imported counts per season name and skipped counts per reason.

    """
    unique = {str(row["PublicMatchId"]): row for row in rows}
    groups: dict[Any, tuple[Season, list[dict]]] = {}
    skipped: list[tuple[dict, str]] = []
    latest: tuple[date, Any] | None = None
    for row in unique.values():
        target, day, reason = route(seasons, row)
        if target is None or day is None:
            skipped.append((row, reason))
            continue
        groups.setdefault(target.pk, (target, []))[1].append(row)
        if latest is None or day > latest[0]:
            latest = (day, target.pk)
    pool = resource.source_id if resource.kind == "edition_pool" else ""
    imported: dict[Any, dict[str, Match]] = {}
    now = timezone.now()
    for key, (target, group) in groups.items():
        importer = Importer(target, now, discover=False)
        if pool:
            # Final standings belong to the season of the poule's last match.
            final = latest is not None and latest[1] == key
            sport = group[0]["HomeTeam"].get("SportId") or ""
            importer.pool({**(group[0].get("Pool") or {}), "PoolId": pool}, sport)
            importer.apply(
                "pool_results",
                pool,
                {
                    "MatchResult": group,
                    "PoolStanding": standing if final else None,
                    "ResultsFiltered": filtered,
                },
            )
        else:
            importer.apply("club_results", "", {"MatchResult": group})
        imported[key] = {
            match.external_id: match
            for match in Match.objects.filter(
                season=target,
                external_id__in=[str(row["PublicMatchId"]) for row in group],
            )
        }
    log_matches(seasons, resource, groups, imported, skipped)
    return {
        "matches": len(unique),
        "imported": {target.name: len(group) for target, group in groups.values()},
        "skipped": dict(Counter(reason for _, reason in skipped)),
    }


def log_matches(
    seasons: EditionSeasons,
    resource: HistoricalResource,
    groups: dict[Any, tuple[Season, list[dict]]],
    imported: dict[Any, dict[str, Match]],
    skipped: list[tuple[dict, str]],
) -> None:
    """Record one checkpoint per match: imported into a season, or skipped."""
    now = timezone.now()
    pool = resource.source_id if resource.kind == "edition_pool" else ""

    def entry(
        season: Season,
        row: dict,
        outcome: tuple[str, str, str],
        evidence: dict[str, object],
    ) -> HistoricalResource:
        identifier = str(row["PublicMatchId"])
        start, end = season.start_date, season.end_date
        state, coverage, reason = outcome
        return HistoricalResource(
            key=resource_key(season, "app", "match", identifier, (start, end)),
            season=season,
            provider="app",
            kind="match",
            source_id=identifier,
            start_date=start,
            end_date=end,
            sport=row["HomeTeam"].get("SportId") or "",
            state=state,
            coverage=coverage,
            reason=reason,
            evidence={"edition": seasons.edition, "pool": pool, **evidence},
            fetched_at=now,
        )

    entries = []
    for key, (target, group) in groups.items():
        for row in group:
            match = imported[key].get(str(row["PublicMatchId"]))
            score = final_score(row)
            outcome = (
                "fetched",
                "complete" if score and match else "partial",
                "" if match else "not_imported",
            )
            entries.append(
                entry(
                    target,
                    row,
                    outcome,
                    {
                        "status": row.get("Status"),
                        "score": list(score) if score else None,
                        "match": match.pk if match else None,
                    },
                )
            )
    # Skipped rows have no playing season; the edition anchor keeps their log.
    entries.extend(
        entry(
            resource.season,
            row,
            ("blocked", "inaccessible", reason),
            {"match_date": row.get("MatchDateTime")},
        )
        for row, reason in skipped
    )
    entries = [row for row in entries if is_match_identity(row.source_id)]
    HistoricalResource.objects.bulk_create(
        entries,
        update_conflicts=True,
        unique_fields=("key",),
        update_fields=("state", "coverage", "reason", "evidence", "fetched_at"),
        batch_size=1000,
    )
    queue_lineups(row for row in entries if row.coverage == "complete")


def queue_lineups(entries: Iterable[HistoricalResource]) -> None:
    """Queue one lineup request per imported final; never reset fetched lineups."""
    lineups = [
        HistoricalResource(
            key=resource_key(
                entry.season,
                "app",
                "lineup",
                entry.source_id,
                (entry.start_date, entry.end_date),
            ),
            season=entry.season,
            provider="app",
            kind="lineup",
            source_id=entry.source_id,
            start_date=entry.start_date,
            end_date=entry.end_date,
            sport=entry.sport,
            evidence={"edition": entry.evidence.get("edition")},
        )
        for entry in entries
    ]
    HistoricalResource.objects.bulk_create(
        lineups, ignore_conflicts=True, batch_size=1000
    )


def queue_edition_lineups(edition: int) -> dict[str, Any]:
    """Queue lineups for an edition imported before lineups were requested.

    Returns:
        The edition and how many lineups were newly queued.

    """
    before = HistoricalResource.objects.filter(
        season__in=edition_scopes(edition), provider="app", kind="lineup"
    ).count()
    entries = (
        HistoricalResource.objects
        .filter(
            season__in=edition_scopes(edition),
            provider="app",
            kind="match",
            state="fetched",
            coverage="complete",
        )
        .select_related("season")
        .iterator(chunk_size=2000)
    )
    queue_lineups(entries)
    after = HistoricalResource.objects.filter(
        season__in=edition_scopes(edition), provider="app", kind="lineup"
    ).count()
    return {"edition": edition, "lineups_queued": after - before}


def is_match_identity(source_id: str) -> bool:
    """Log only app match identities; malformed IDs are never stored."""
    try:
        validate_identity("app", "match", source_id)
    except ValueError:
        return False
    return True


def _counts(queryset: QuerySet, fields: tuple[str, ...]) -> list[tuple]:
    """Group a queryset by fields and count rows."""
    return list(queryset.values_list(*fields).annotate(n=Count("pk")).order_by(*fields))


def edition_summary(edition: int) -> dict[str, Any]:
    """Report discovery progress and the match log for one edition."""
    resources = HistoricalResource.objects.filter(
        season__in=edition_scopes(edition), provider="app"
    )
    discovery: dict[str, dict[str, int]] = defaultdict(dict)
    for kind, state, coverage, count in _counts(
        resources.filter(kind__in=EDITION_KINDS), ("kind", "state", "coverage")
    ):
        discovery[kind][f"{state}/{coverage}"] = count
    matches = resources.filter(kind="match")
    released = set(
        HistoricalDiscovery.objects
        .filter(resource__in=resources, reference__startswith=f"catalogue/{edition}/")
        .values_list("reference", flat=True)
        .distinct()
    )
    return {
        "edition": edition,
        "scan_window": next(
            (
                row.evidence
                for row in resources.filter(kind="edition_scan").only("evidence")
            ),
            None,
        ),
        "strata_released": sorted(
            label.removeprefix(f"catalogue/{edition}/") for label in released
        ),
        "discovery": dict(discovery),
        "matches_imported": dict(
            _counts(matches.filter(state="fetched"), ("season__name",))
        ),
        "matches_skipped": dict(_counts(matches.filter(state="blocked"), ("reason",))),
        "lineups": {
            f"{state}/{reason}" if reason else state: count
            for state, reason, count in _counts(
                resources.filter(kind="lineup"), ("state", "reason")
            )
        },
    }


LOG_FIELDS = (
    "edition",
    "season",
    "match_id",
    "date",
    "pool",
    "home_team",
    "away_team",
    "status",
    "home_score",
    "away_score",
    "outcome",
    "reason",
    "logged_at",
)


def edition_log(edition: int) -> Iterator[dict[str, Any]]:
    """Read the per-match import log of one edition.

    Yields:
        One row per logged match, oldest match first.

    """
    entries = list(
        HistoricalResource.objects.filter(
            season__in=edition_scopes(edition), provider="app", kind="match"
        ).select_related("season")
    )
    matches = {
        match.pk: match
        for match in Match.objects.filter(
            pk__in=[
                entry.evidence["match"]
                for entry in entries
                if entry.evidence.get("match")
            ]
        ).select_related("home_team", "away_team", "pool")
    }
    rows = []
    for entry in entries:
        match = matches.get(entry.evidence.get("match"))
        rows.append({
            "edition": edition,
            "season": entry.season.name if entry.state == "fetched" else "",
            "match_id": entry.source_id,
            "date": (
                timezone.localtime(match.starts_at).isoformat()
                if match
                else entry.evidence.get("match_date") or ""
            ),
            "pool": (
                f"{match.pool.class_name} {match.pool.name}".strip()
                if match and match.pool
                else entry.evidence.get("pool") or ""
            ),
            "home_team": match.home_team.name if match else "",
            "away_team": match.away_team.name if match else "",
            "status": match.status if match else "",
            "home_score": match.home_score if match else None,
            "away_score": match.away_score if match else None,
            "outcome": "imported" if entry.state == "fetched" else "skipped",
            "reason": entry.reason,
            "logged_at": entry.fetched_at.isoformat() if entry.fetched_at else "",
        })
    yield from sorted(rows, key=lambda row: (str(row["date"]), row["match_id"]))
