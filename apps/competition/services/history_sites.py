"""Seasons the KNKV app no longer serves, read from two public result sites.

``korfbalnl`` is KNKV's former competition site (competitie.korfbal.nl), which
still holds November 2016 to June 2022. ``uitslagen`` is korfbal-uitslagen.nl,
which holds 2025-2026. Both use Sportlink's poule numbers and club codes, so
their results land in the same poules and clubs as provider data. Their matches
keep an ``archive:`` ID: they have no lineups, and the provider's own record
replaces them when the app serves the poule again.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from datetime import date, timedelta
import re
from typing import Any

from django.db import transaction
from django.db.models import Count, Exists, OuterRef, Q, QuerySet
from django.utils import timezone

from apps.competition.models import (
    Club,
    HistoricalDiscovery,
    HistoricalResource,
    Match,
    Pool,
    Team,
)
from apps.competition.services.clock_twins import untouched
from apps.competition.services.computed_standings import (
    refresh_computed_standings,
)
from apps.competition.services.history import (
    ARCHIVE_PREFIX,
    EDITION_KINDS,
    SITE_PROVIDERS,
    resource_key,
    validate_identity,
)
from apps.competition.services.history_editions import (
    SPORTS,
    edition_scopes,
    edition_seasons,
    pool_phases,
    prepare_edition,
    route,
)
from apps.competition.services.importer import Importer
from apps.competition.services.seasons import OUTDOOR
from apps.schedule.domain.competition_context import FULL_SEASON, edition_bounds
from apps.schedule.models import (
    Match as AppMatch,
    Season,
)


KORFBALNL, UITSLAGEN = "korfbalnl", "uitslagen"
# Rows per korfbal-uitslagen.nl page: its API returns at most this many.
PAGE_SIZE = 1000
NAMESPACES = {KORFBALNL: "knkv", UITSLAGEN: "ku"}
REFERENCES = {
    KORFBALNL: "https://competitie.korfbal.nl/",
    UITSLAGEN: "https://korfbal-uitslagen.nl/",
}
# The first checkpoint of each source and its fixed identifier (None: the edition).
FIRST_CHECKPOINT = {KORFBALNL: ("catalogue", None), UITSLAGEN: ("match_page", "0")}
PLAYED = "uitgespeeld"
# The former KNKV site's series for a competition that plays the whole edition.
FULL_YEAR_SERIE = "REGULIER"
# Catalogues without each poule's series cannot place full-year poules.
CATALOGUE_VERSION = 2
SIDES = ("home", "away")


def edition_interval(anchor: Season) -> tuple[date, date]:
    """Return an edition's first and last day, never later than yesterday."""
    edition = anchor.start_date.year
    return (
        date(edition, 7, 1),
        min(date(edition + 1, 6, 30), timezone.localdate() - timedelta(days=1)),
    )


def queue(
    anchor: Season,
    provider: str,
    kind: str,
    source_ids: Iterable[str],
    *,
    parent: HistoricalResource | None,
) -> int:
    """Queue site checkpoints in bulk; completed work is never reset.

    Returns:
        The number of newly queued checkpoints.

    """
    start, end = edition_interval(anchor)
    keys = {}
    for source_id in source_ids:
        validate_identity(provider, kind, source_id)
        keys[resource_key(anchor, provider, kind, source_id, (start, end))] = source_id
    existing = set(
        HistoricalResource.objects.filter(key__in=keys).values_list("key", flat=True)
    )
    HistoricalResource.objects.bulk_create(
        [
            HistoricalResource(
                key=key,
                season=anchor,
                provider=provider,
                kind=kind,
                source_id=source_id,
                start_date=start,
                end_date=end,
            )
            for key, source_id in keys.items()
            if key not in existing
        ],
        ignore_conflicts=True,
        batch_size=1000,
    )
    HistoricalDiscovery.objects.bulk_create(
        [
            HistoricalDiscovery(
                resource_id=pk, parent=parent, reference=REFERENCES[provider]
            )
            for pk in HistoricalResource.objects.filter(key__in=keys).values_list(
                "pk", flat=True
            )
        ],
        ignore_conflicts=True,
        batch_size=1000,
    )
    return len(keys.keys() - existing)


def seed_site(provider: str, edition: int) -> dict[str, Any]:
    """Queue one edition from a public result site.

    The app stays the preferred source: an edition whose app discovery is still
    running waits, so the site only fills poules the app did not deliver.

    Raises:
        ValueError: The source is unknown or the edition is not finished.

    """
    if provider not in SITE_PROVIDERS:
        raise ValueError("Unknown public result site")
    anchor = prepare_edition(edition).indoor
    result = {"edition": edition, "source": provider, "queued": 0}
    if HistoricalResource.objects.filter(
        season=anchor, provider="app", kind__in=EDITION_KINDS, state="pending"
    ).exists():
        return {**result, "reason": "app_discovery_pending"}
    kind, source_id = FIRST_CHECKPOINT[provider]
    result["queued"] = queue(
        anchor, provider, kind, [source_id or str(edition)], parent=None
    )
    for catalogue in HistoricalResource.objects.filter(
        season=anchor, provider=provider, kind="catalogue", state="fetched"
    ):
        if catalogue.evidence.get("version") != CATALOGUE_VERSION:
            catalogue.state, catalogue.next_attempt_at = "pending", timezone.now()
            catalogue.save(update_fields=("state", "next_attempt_at"))
    # Read clubs again whose rows an earlier version skipped for a delisted club
    # or for a team without a Sportlink code.
    result["requeued"] = (
        HistoricalResource.objects
        .filter(season=anchor, provider=provider, kind="club_matches", state="fetched")
        .filter(
            Q(evidence__skipped__club_unknown__gt=0)
            | Q(evidence__skipped__incomplete__gt=0)
        )
        .update(state="pending", attempts=0, next_attempt_at=timezone.now())
    )
    return result


def team_name(name: str, source_club: str, club: Club | None) -> str:
    """Name a team after its catalogue club, whatever sponsor the site lists.

    Publication recognises a club's team by the club name before its designation;
    a sponsor name from another year would otherwise publish a second team.
    """
    prefix = source_club.strip() + " "
    if club is None or not club.name or not prefix.strip():
        return name
    return f"{club.name} {name[len(prefix) :]}" if name.startswith(prefix) else name


def side_payload(
    provider: str, team: dict[str, Any], club: dict[str, Any], sport: str
) -> dict[str, Any]:
    """Build one team of a normalized result row."""
    identifier = str(team.get("ref_id") or "")
    if provider == KORFBALNL:
        # Numeric Sportlink team codes are not the app's public team IDs. Teams
        # the site added in spring 2018 have no code, only the site's own number.
        identifier = (
            f"{ARCHIVE_PREFIX}{NAMESPACES[provider]}:{identifier or team['_id']}"
        )
    return {
        "PublicTeamId": identifier,
        "TeamName": team["name"],
        "SportId": sport,
        "Club": {
            "ClubId": club["ref_id"],
            "ClubName": club["name"],
            "City": club.get("city") or "",
            **({"Dissolved": True} if club.get("dissolved") else {}),
        },
    }


def score(value: object) -> int | None:
    """Accept only a real, nonnegative score."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def result(sides: list[dict], scores: list[int | None]) -> dict[str, Any]:
    """Pair both teams with their final scores."""
    return {
        "HomeTeam": sides[0],
        "AwayTeam": sides[1],
        "HomeResult": {"Score": scores[0]},
        "AwayResult": {"Score": scores[1]},
    }


def payload(
    provider: str, identifier: object, when: str, pool: dict[str, Any], played: dict
) -> dict[str, Any]:
    """Build a final result in the provider's row shape, with an archive ID."""
    return {
        "PublicMatchId": f"{ARCHIVE_PREFIX}{NAMESPACES[provider]}:{identifier}",
        "MatchDateTime": when,
        "Status": "FINAL",
        "AutoResult": None,
        "Pool": pool,
        **played,
    }


def site_clubs(
    catalogue: dict[str, Any], rows: list[dict[str, Any]]
) -> tuple[dict[str, dict[str, Any]], set[str]]:
    """Identify every club in the rows, including clubs the site no longer lists.

    The site keeps the matches and name of a club that merged or dissolved but
    not its Sportlink code. Such a club is the catalogue club with exactly that
    name, or else a dissolved club of its own under the site's club number.

    Returns:
        Clubs by site club number, and the numbers of the delisted clubs.

    """
    clubs = {
        key: dict(zip(("ref_id", "name", "city"), club, strict=True))
        for key, club in catalogue["clubs"].items()
    }
    delisted = {
        club["_id"]: (club.get("name") or "").strip()
        for row in rows
        for club in ((row.get("clubs") or {}).get(side) or {} for side in SIDES)
        if club.get("_id") and club["_id"] not in clubs
    }
    named: dict[str, list[str]] = {}
    for external_id, name in Club.objects.filter(
        name__in={name for name in delisted.values() if name}
    ).values_list("external_id", "name"):
        named.setdefault(name, []).append(external_id)
    for key, name in delisted.items():
        if not name:
            continue
        matches = named.get(name, [])
        clubs[key] = (
            {"ref_id": matches[0], "name": name}
            if len(matches) == 1
            else {
                "ref_id": f"{ARCHIVE_PREFIX}{NAMESPACES[KORFBALNL]}:{key}",
                "name": name,
                "dissolved": True,
            }
        )
    return clubs, {key for key, name in delisted.items() if name}


def korfbalnl_row(
    row: dict[str, Any], catalogue: dict[str, Any], clubs: dict[str, dict[str, Any]]
) -> dict | str:
    """Normalize one competitie.korfbal.nl match, or name the reason to skip it."""
    if str((row.get("status") or {}).get("game") or "").casefold() != PLAYED:
        return "not_played"
    sport = catalogue["sports"].get((row.get("sport") or {}).get("_id"))
    if sport not in SPORTS:
        return "unsupported_sport"
    sides_clubs = [
        club
        for side in SIDES
        if (club := clubs.get((row["clubs"][side] or {}).get("_id")))
    ]
    if len(sides_clubs) != len(SIDES):
        return "club_unknown"
    stats = row.get("stats") or {}
    scores = [score((stats.get(side) or {}).get("score")) for side in SIDES]
    poule = row.get("poule") or {}
    teams = [row["teams"][side] for side in SIDES]
    if None in scores or not all(
        (team.get("ref_id") or team.get("_id")) and team.get("name") for team in teams
    ):
        return "incomplete"
    if not re.fullmatch(r"[0-9]+", str(poule.get("ref_id") or "")):
        return "poule_unknown"
    name, class_name, serie = catalogue["pools"].get(str(poule["ref_id"])) or (
        poule.get("name") or "",
        "",
        "",
    )
    sides = [
        side_payload(KORFBALNL, team, club, sport)
        for team, club in zip(teams, sides_clubs, strict=True)
    ]
    return payload(
        KORFBALNL,
        row.get("ref_id") or row["_id"],
        row["date"],
        {
            "PoolId": str(poule["ref_id"]),
            "PoolName": name,
            "ClassName": class_name,
            # One competition over both outdoor halves stays whole (see route).
            "FullYear": serie == FULL_YEAR_SERIE and sport == OUTDOOR,
        },
        result(sides, scores),
    )


def uitslagen_row(row: dict[str, Any]) -> dict | str:
    """Normalize one korfbal-uitslagen.nl match, or name the reason to skip it."""
    scores = [score(row.get("home_score")), score(row.get("away_score"))]
    if None in scores:
        return "not_played"
    pool = row.get("pool") or {}
    sport = ((pool.get("phase") or {}).get("sport") or {}).get("ref_id")
    if sport not in SPORTS:
        return "unsupported_sport"
    if not re.fullmatch(r"[0-9]+", str(pool.get("ref_id") or "")):
        return "poule_unknown"
    teams = [row.get(side) or {} for side in ("home", "away")]
    if not all(
        re.fullmatch(r"T[0-9]+", str(team.get("ref_id") or ""))
        and (team.get("club") or {}).get("ref_id")
        for team in teams
    ):
        return "team_unknown"
    sides = [
        side_payload(
            UITSLAGEN,
            team,
            {"ref_id": team["club"]["ref_id"], "name": team["club"].get("name") or ""},
            sport,
        )
        for team in teams
    ]
    return payload(
        UITSLAGEN,
        row["id"],
        row["date"],
        {
            "PoolId": str(pool["ref_id"]),
            "PoolName": pool.get("name") or "",
            "ClassName": (pool.get("division") or {}).get("name") or "",
        },
        result(sides, scores),
    )


def apply_catalogue(resource: HistoricalResource, data: dict[str, Any]) -> None:
    """Keep the site's club, sport and poule names and queue one read per club."""
    clubs = {
        row["_id"]: [
            row["ref_id"],
            row.get("name") or "",
            (row.get("address") or {}).get("city") or "",
        ]
        for row in data["clubs"]
        if row.get("ref_id")
    }
    resource.evidence = {
        "version": CATALOGUE_VERSION,
        "sports": {row["_id"]: row.get("ref_id") for row in data["sports"]},
        "clubs": clubs,
        "pools": {
            str(row["ref_id"]): [
                row.get("name") or "",
                (row.get("division") or {}).get("name") or "",
                row.get("serie") or "",
            ]
            for row in data["poules"]
            if row.get("ref_id")
        },
    }
    queue(resource.season, KORFBALNL, "club_matches", sorted(clubs), parent=resource)
    resource.coverage = "complete" if clubs else "empty"


def covered_pools(scopes: list[Season], pool_ids: set[str]) -> set[str]:
    """Poules the provider delivered matches for, in any season of the edition.

    The provider keeps a full-year outdoor poule in its own season, so a poule
    absent from the autumn or spring season can still be covered.
    """
    return set(
        Match.objects
        .filter(season__in=scopes, pool__external_id__in=pool_ids)
        .exclude(external_id__startswith=ARCHIVE_PREFIX)
        .values_list("pool__external_id", flat=True)
        .distinct()
    )


@transaction.atomic
def remove_site_matches(query: QuerySet[Match]) -> set[str]:
    """Remove site matches with the fixtures and empty poules they created.

    Returns:
        IDs of the matches kept because their fixture carries native activity.

    """
    matches = list(query.select_for_update(of=("self",)).select_related("local_match"))
    kept = {
        match.external_id
        for match in matches
        if match.local_match is not None
        and not (match.local_created and untouched(match.local_match))
    }
    gone = [match for match in matches if match.external_id not in kept]
    keys = [match.pk for match in gone]
    fixtures = [match.local_match_id for match in gone if match.local_match_id]
    Match.objects.filter(pk__in=keys).update(local_match=None)
    AppMatch.objects.filter(pk__in=fixtures).delete()
    Match.objects.filter(pk__in=keys).delete()
    for pool in (
        Pool.objects
        .filter(pk__in={match.pool_id for match in gone if match.pool_id})
        .exclude(Exists(Match.objects.filter(pool=OuterRef("pk"))))
        .select_related("local_pool")
    ):
        local = pool.local_pool
        pool.delete()
        if local is not None and not AppMatch.objects.filter(pool=local).exists():
            local.delete()
    return kept


def import_site_rows(
    resource: HistoricalResource, rows: list[dict | str]
) -> dict[str, Any]:
    """Import normalized rows into their playing seasons; the app's poules win.

    Returns:
        Imported counts per season name and skipped counts per reason.

    """
    seasons = edition_seasons(resource)
    skipped: Counter[str] = Counter(row for row in rows if isinstance(row, str))
    unique = {row["PublicMatchId"]: row for row in rows if isinstance(row, dict)}
    clubs = {
        club.external_id: club
        for club in Club.objects.filter(
            external_id__in={
                row[side]["Club"]["ClubId"]
                for row in unique.values()
                for side in ("HomeTeam", "AwayTeam")
            }
        )
    }
    # Sportlink's public team IDs belong to one discipline; a site must not move
    # a team. The former KNKV site's team codes cover both disciplines.
    sports = dict(
        Team.objects.filter(
            external_id__in={
                row[side]["PublicTeamId"]
                for row in unique.values()
                for side in ("HomeTeam", "AwayTeam")
                if not row[side]["PublicTeamId"].startswith(ARCHIVE_PREFIX)
            }
        ).values_list("external_id", "sport")
    )
    covered = covered_pools(
        edition_scopes(seasons.edition),
        {row["Pool"]["PoolId"] for row in unique.values()},
    )
    halves = {seasons.autumn.pk, seasons.spring.pk}
    moved: list[str] = []
    groups: dict[Any, tuple[Season, list[dict]]] = {}
    # Rows of one poule share its period; a site's full-year flag is evidence.
    phases = pool_phases(
        seasons,
        unique.values(),
        {
            str(row["Pool"]["PoolId"]): FULL_SEASON
            for row in unique.values()
            if row["Pool"].get("FullYear")
        },
    )
    for row in unique.values():
        phase = phases.get(str(row["Pool"]["PoolId"]))
        target, _, reason = route(seasons, row, phase)
        if target is not None and any(
            sports.get(row[side]["PublicTeamId"], row[side]["SportId"])
            != row[side]["SportId"]
            for side in ("HomeTeam", "AwayTeam")
        ):
            target, reason = None, "sport_mismatch"
        if target is not None and row["Pool"]["PoolId"] in covered:
            target, reason = None, "app_has_poule"
        full_year = row["Pool"].pop("FullYear", False)
        if target is None:
            skipped[reason] += 1
            continue
        if full_year and target.phase == FULL_SEASON:
            # An earlier read may have placed this fixture in a half.
            moved.append(row["PublicMatchId"])
        for side in ("HomeTeam", "AwayTeam"):
            team = row[side]
            team["TeamName"] = team_name(
                team["TeamName"],
                team["Club"]["ClubName"],
                clubs.get(team["Club"]["ClubId"]),
            )
        groups.setdefault(target.pk, (target, []))[1].append(row)
    # An earlier read placed full-year poules in the halves: retire those copies.
    in_use = (
        remove_site_matches(
            Match.objects.filter(season_id__in=halves, external_id__in=moved)
        )
        if moved
        else set()
    )
    imported = {}
    now = timezone.now()
    for target, group in groups.values():
        fresh = [row for row in group if row["PublicMatchId"] not in in_use]
        skipped["half_copy_in_use"] += len(group) - len(fresh)
        if fresh:
            Importer(
                target, now, discover=False, window=edition_bounds(seasons.edition)
            ).apply("club_results", "", {"MatchResult": fresh})
            imported[target.name] = len(fresh)
            # A site has results but no standings: compute the poules' tables.
            refresh_computed_standings(
                Pool.objects.filter(
                    season=target,
                    external_id__in={row["Pool"]["PoolId"] for row in fresh},
                ).values_list("pk", flat=True)
            )
    return {"imported": imported, "skipped": dict(+skipped)}


def apply_site(resource: HistoricalResource, data: dict[str, Any]) -> None:
    """Apply one public result site response.

    Raises:
        ValueError: The checkpoint kind does not belong to a result site.

    """
    if resource.kind == "catalogue":
        apply_catalogue(resource, data)
        return
    if resource.kind == "club_matches":
        catalogue = HistoricalResource.objects.get(
            season=resource.season, provider=KORFBALNL, kind="catalogue"
        ).evidence
        if catalogue.get("version") != CATALOGUE_VERSION:
            raise ValueError("The catalogue must be read again before its clubs")
        matches = [row for week in data["weeks"] for row in week["matches"]]
        clubs, delisted = site_clubs(catalogue, matches)
        # The catalogue queued listed clubs only; a delisted club's own matches
        # against other delisted clubs appear in no other read.
        queue(
            resource.season,
            KORFBALNL,
            "club_matches",
            sorted(delisted),
            parent=resource,
        )
        rows = [korfbalnl_row(row, catalogue, clubs) for row in matches]
    elif resource.kind == "match_page":
        rows = [uitslagen_row(row) for row in data["rows"]]
        if len(rows) >= PAGE_SIZE:
            # Pages follow the site's match number; the last one is the cursor.
            queue(
                resource.season,
                UITSLAGEN,
                "match_page",
                [str(data["rows"][-1]["id"])],
                parent=resource,
            )
    else:
        raise ValueError("Unsupported result site resource")
    resource.evidence = {"rows": len(rows), **import_site_rows(resource, rows)}
    # A site is not the provider of record: its coverage is never called complete.
    resource.coverage = "partial" if resource.evidence["imported"] else "empty"


def site_summary(edition: int) -> dict[str, Any]:
    """Report site checkpoints and archive matches of one edition."""
    scopes = edition_scopes(edition)
    resources = HistoricalResource.objects.filter(
        season__in=scopes, provider__in=SITE_PROVIDERS
    )
    checkpoints: dict[str, dict[str, int]] = {}
    for provider, kind, state, count in (
        resources
        .values_list("provider", "kind", "state")
        .annotate(n=Count("pk"))
        .order_by("provider", "kind", "state")
    ):
        checkpoints.setdefault(provider, {})[f"{kind}/{state}"] = count
    skipped: Counter[str] = Counter()
    for evidence in resources.exclude(kind="catalogue").values_list(
        "evidence", flat=True
    ):
        skipped.update(evidence.get("skipped") or {})
    return {
        "checkpoints": checkpoints,
        "matches": dict(
            Match.objects
            .filter(season__in=scopes, external_id__startswith=ARCHIVE_PREFIX)
            .values_list("season__name")
            .annotate(n=Count("pk"))
            .order_by("season__name")
        ),
        "skipped": dict(skipped),
    }
