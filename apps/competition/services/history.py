"""Historical graph discovery, conservative coverage, and explicit season boundaries."""

from __future__ import annotations

from datetime import date, timedelta
import hashlib
import json
import re
from typing import Any, TypedDict, Unpack
from urllib.parse import urlsplit

from django.db import transaction
from django.db.models import Count
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.competition.domain.history_scopes import ARCHIVE_PREFIX
from apps.competition.domain.standings_provenance import is_official_standing
from apps.competition.models import (
    HistoricalDiscovery,
    HistoricalResource,
    Match,
    MatchMembership,
    Pool,
    PoolEntry,
)
from apps.competition.services.history_integrity import (
    fixture_coverage,
    standing_projection,
    unique_matches,
)
from apps.competition.services.importer import Importer
from apps.competition.services.lineups import import_lineup
from apps.competition.services.seasons import INDOOR
from apps.schedule.domain.competition_context import INDOOR_PHASE
from apps.schedule.models import Season


# Public result sites that still hold seasons the app no longer serves: KNKV's
# former competition site and korfbal-uitslagen.nl (see history_sites.py).
SITE_PROVIDERS = {"korfbalnl", "uitslagen"}
PROVIDERS = {"app", "dataservice", "archive", *SITE_PROVIDERS}
KINDS = {
    "match",
    "pool",
    "window",
    "pool_window",
    "standing",
    "members",
    "edition_team",
    "edition_pool",
    "edition_scan",
    "lineup",
    "catalogue",
    "club_matches",
    "match_page",
}
# Season-scoped app discovery: a team's poules, then a poule's results. The
# scan marker is never fetched; it records an edition's poule scan window.
EDITION_KINDS = {"edition_team", "edition_pool"}
ROW_LIMIT = 1000
DATA_ROW_LIMITS = {"window": 500, "pool_window": ROW_LIMIT}
MAX_REFERENCE_LENGTH = 512


class HistoryUnavailableError(ValueError):
    """A permanent coverage limitation; retain its short, non-sensitive code."""


def reference_label(value: str) -> str:
    """Keep archive attribution without storing URL credentials or query tokens.

    Raises:
        ValueError: The supplied configuration or response is inconsistent.

    """
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc:
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            raise ValueError("Use an HTTPS source without credentials")
        value = f"https://{parsed.netloc}{parsed.path}"
    if not value or len(value) > MAX_REFERENCE_LENGTH:
        raise ValueError("Provide a source reference of at most 512 characters")
    return value


def validate_identity(provider: str, kind: str, source_id: str) -> None:
    """Reject unsupported routes and malformed IDs before queueing work.

    Raises:
        ValueError: The requested resource is not a supported provider identity.

    """
    if provider not in PROVIDERS or kind not in KINDS:
        raise ValueError("Unsupported historical provider or resource")
    if provider == "app" and kind not in {
        "match",
        "lineup",
        "pool",
        "edition_scan",
        *EDITION_KINDS,
    }:
        raise ValueError("The app has no verified historical date filter")
    if not re.fullmatch(r"[A-Za-z0-9:_-]{1,80}", source_id):
        raise ValueError("Provide a provider identifier, not a URL or credential")
    if provider == "app":
        pattern = {
            "match": r"M[0-9]+",
            "lineup": r"M[0-9]+",
            "edition_team": r"T[0-9]+",
        }.get(kind, r"[0-9]+")
        if not re.fullmatch(pattern, source_id):
            raise ValueError("Use the original app match or pool identifier")


class SeedOptions(TypedDict, total=False):
    """Optional scope and provenance for an observed historical identifier."""

    start: date | None
    end: date | None
    sport: str
    reference: str
    parent: HistoricalResource | None


def resource_key(
    season: Season,
    provider: str,
    kind: str,
    source_id: str,
    interval: tuple[date, date],
) -> str:
    """Hash one checkpoint identity; match/poule IDs are shared across windows."""
    identity = [str(season.pk), provider, kind, source_id]
    if kind in {"window", "pool_window"}:
        identity += [str(interval[0]), str(interval[1])]
    return hashlib.sha256(json.dumps(identity).encode()).hexdigest()


def seed(
    season: Season,
    provider: str,
    kind: str,
    source_id: str,
    **options: Unpack[SeedOptions],
) -> HistoricalResource:
    """Discover once; never reset completed work when another feed links to it.

    Raises:
        ValueError: The supplied configuration or response is inconsistent.

    """
    start, end = options.get("start"), options.get("end")
    sport = options.get("sport", "")
    reference = options.get("reference", "operator")
    parent = options.get("parent")
    validate_identity(provider, kind, source_id)
    start, end = start or season.start_date, end or season.end_date
    if not season.start_date <= start <= end <= season.end_date:
        raise ValueError(
            "Historical dates must belong to the explicitly selected season"
        )
    if start >= timezone.localdate() or end >= timezone.localdate():
        raise ValueError("Historical intervals must finish before today")
    key = resource_key(season, provider, kind, source_id, (start, end))
    with transaction.atomic():
        resource, created = (
            HistoricalResource.objects.select_for_update().get_or_create(
                key=key,
                defaults={
                    "season": season,
                    "provider": provider,
                    "kind": kind,
                    "source_id": source_id,
                    "start_date": start,
                    "end_date": end,
                    "sport": sport,
                },
            )
        )
        if not created and sport and resource.sport and sport != resource.sport:
            raise ValueError("Conflicting historical sport identity")
        changed = []
        if not resource.sport and sport:
            resource.sport = sport
            changed.append("sport")
        if not created and kind not in {"window", "pool_window"}:
            bounds = (min(resource.start_date, start), max(resource.end_date, end))
            if bounds != (resource.start_date, resource.end_date):
                resource.start_date, resource.end_date = bounds
                changed.extend(("start_date", "end_date"))
                # The same ID keeps one checkpoint, but a newly observed scope can
                # make a formerly rejected match valid. Fetched IDs need no repeat.
                if (
                    resource.state == "blocked"
                    and resource.reason == "interval_mismatch"
                ):
                    resource.state, resource.coverage, resource.reason = (
                        "pending",
                        "unknown",
                        "",
                    )
                    resource.etag = ""
                    resource.attempts = 0
                    resource.next_attempt_at = timezone.now()
                    changed.extend((
                        "state",
                        "coverage",
                        "reason",
                        "etag",
                        "attempts",
                        "next_attempt_at",
                    ))
        if changed:
            resource.save(update_fields=changed)
        HistoricalDiscovery.objects.get_or_create(
            resource=resource,
            reference=reference_label(reference),
            defaults={"parent": parent},
        )
    return resource


def discover(
    resource: HistoricalResource, kind: str, identifier: str
) -> HistoricalResource:
    """Follow an observed provider ID, preserving the provenance edge."""
    return seed(
        resource.season,
        resource.provider,
        kind,
        identifier,
        start=resource.start_date,
        end=resource.end_date,
        sport=resource.sport,
        reference=f"resource/{resource.pk}",
        parent=resource,
    )


def split_window(resource: HistoricalResource) -> None:
    """Partition inclusive dates; leave truncated single-day results partial."""
    if resource.start_date == resource.end_date:
        resource.state, resource.coverage, resource.reason = (
            "blocked",
            "partial",
            "single_day_row_limit",
        )
        return
    midpoint = resource.start_date + (resource.end_date - resource.start_date) // 2
    for start, end in [
        (resource.start_date, midpoint),
        (midpoint + timedelta(days=1), resource.end_date),
    ]:
        child = seed(
            resource.season,
            resource.provider,
            resource.kind,
            resource.source_id,
            start=start,
            end=end,
            sport=resource.sport,
            reference=f"resource/{resource.pk}",
            parent=resource,
        )
        if (
            resource.state == "pending"
            and resource.fetched_at
            and child.state != "pending"
        ):
            # An explicit parent recheck must revisit previously completed child
            # intervals, even when the parent is still too large to fetch whole.
            child.state, child.coverage, child.reason = "pending", "unknown", ""
            child.evidence, child.etag, child.attempts = {}, "", 0
            child.next_attempt_at = timezone.now()
            child.fetched_at = child.fetched_at or resource.fetched_at
            child.save(
                update_fields=(
                    "state",
                    "coverage",
                    "reason",
                    "evidence",
                    "etag",
                    "attempts",
                    "next_attempt_at",
                    "fetched_at",
                )
            )
    resource.state, resource.coverage, resource.reason = (
        "split",
        "partial",
        "window_split",
    )


def validate_match(row: dict[str, Any], resource: HistoricalResource) -> None:
    """Reject wrong-season responses before creating any source or native record.

    Raises:
        ValueError: The supplied configuration or response is inconsistent.
        HistoryUnavailableError: The historical scope cannot be established.

    """
    stamp = parse_datetime(row["MatchDateTime"])
    if stamp is None or timezone.is_naive(stamp):
        raise ValueError("Historical match timestamp needs an offset")
    match_date = timezone.localdate(stamp)
    if not resource.season.start_date <= match_date <= resource.season.end_date:
        raise HistoryUnavailableError("season_mismatch")
    if (
        resource.kind == "match"
        and not resource.start_date <= match_date <= resource.end_date
    ):
        raise HistoryUnavailableError("interval_mismatch")
    if match_date >= timezone.localdate():
        raise HistoryUnavailableError("not_historical")
    if resource.sport and any(
        row[side].get("SportId") != resource.sport for side in ("HomeTeam", "AwayTeam")
    ):
        raise HistoryUnavailableError("sport_mismatch")


def apply_lineup(resource: HistoricalResource, data: dict[str, Any]) -> None:
    """Store who played an imported match, with the live sync's privacy rules.

    Raises:
        HistoryUnavailableError: The response does not match the imported fixture.

    """
    try:
        import_lineup(resource.season, resource.source_id, data, timezone.now())
    except (ValueError, TypeError) as exc:
        raise HistoryUnavailableError("lineup_invalid") from exc
    resource.coverage = "complete"
    resource.evidence = {
        **resource.evidence,
        "people": MatchMembership.objects.filter(
            match__season=resource.season, match__external_id=resource.source_id
        ).count(),
    }


def apply_app(resource: HistoricalResource, data: dict[str, Any]) -> None:
    """Reuse historical source models without queuing present-day club/team feeds.

    Raises:
        ValueError: The resource kind is unsupported.

    """
    if resource.kind == "lineup":
        apply_lineup(resource, data)
    elif resource.kind == "match":
        apply_app_match(resource, data)
    elif resource.kind == "pool":
        apply_app_pool(resource, data)
    else:
        raise ValueError("Unsupported app history resource")


def apply_app_match(resource: HistoricalResource, data: dict[str, Any]) -> None:
    """Observe one scoped detail and refresh its old and new pool proofs.

    Raises:
        ValueError: The response names another match.

    """
    importer = Importer(resource.season, timezone.now(), discover=False)
    if str(data["PublicMatchId"]) != resource.source_id:
        raise ValueError("Unexpected match identity")
    validate_match(data, resource)
    prior_pools = set(
        Match.objects
        .filter(season=resource.season, external_id=resource.source_id)
        .exclude(pool=None)
        .values_list("pool__external_id", flat=True)
    )
    importer.apply("club_results", "", {"MatchResult": [data]})
    if data.get("Pool"):
        pool_resource = discover(resource, "pool", str(data["Pool"]["PoolId"]))
        prior_pools.add(pool_resource.source_id)
    refresh_app_pool_coverage(resource.season, prior_pools)
    resource.coverage = "partial"
    resource.evidence = {
        **resource.evidence,
        "matches": len(importer.observed_match_ids),
        "match_ids": [resource.source_id],
        "detail_attempted": True,
    }


def apply_app_pool(resource: HistoricalResource, data: dict[str, Any]) -> None:
    """Import a validated pool response and retain independent count evidence.

    Raises:
        ValueError: A response fixture names another pool.

    """
    importer = Importer(resource.season, timezone.now(), discover=False)
    rows = unique_matches(data["MatchResult"])
    table = standing_projection(data.get("PoolStanding"))
    if table is None:
        table = standing_projection(resource.evidence.get("official_table"))
    data = {**data, "MatchResult": rows, "PoolStanding": table}
    for row in rows:
        validate_match(row, resource)
        if row.get("Pool") and str(row["Pool"]["PoolId"]) != resource.source_id:
            raise ValueError("Unexpected poule identity")
        if not row.get("Pool"):
            # Enclosing-pool evidence must take the same contextual update path
            # as a detail response, including metadata and table invalidation.
            row["Pool"] = {"PoolId": resource.source_id}
    # Undated standings alone cannot establish that a reused pool ID is historical.
    if not rows:
        retain_app_standings(resource, data, table, importer)
        return
    prior_pools = set(
        Match.objects
        .filter(
            season=resource.season,
            external_id__in=[str(row["PublicMatchId"]) for row in rows],
        )
        .exclude(pool=None)
        .exclude(pool__external_id=resource.source_id)
        .values_list("pool__external_id", flat=True)
    )
    importer.pool({"PoolId": resource.source_id}, resource.sport)
    importer.apply("pool_results", resource.source_id, data)
    resource.evidence = {
        **resource.evidence,
        "accepted_match_ids": [str(row["PublicMatchId"]) for row in rows],
        "source_accounted": len(importer.observed_match_ids) == len(rows),
        "official_table": table
        if table is not None
        else resource.evidence.get("official_table"),
    }
    resource.coverage, resource.evidence = pool_coverage(resource, data)
    resource.reason = (
        "" if resource.coverage == "complete" else "standings_results_disagree"
    )
    reuse_pool_matches(resource, [str(row["PublicMatchId"]) for row in rows])
    prior_pools.add(resource.source_id)
    refresh_app_pool_coverage(resource.season, prior_pools)


def retain_app_standings(
    resource: HistoricalResource,
    data: dict[str, Any],
    table: dict | None,
    importer: Importer,
) -> None:
    """Store an undated table, using an established or proven indoor binding."""
    pool = Pool.objects.filter(
        season=resource.season, external_id=resource.source_id
    ).first()
    standing_rows = (table or {}).get("PoolStandingTeam") or []
    if (
        pool is None
        and resource.season.context.phase == INDOOR_PHASE
        and {row.get("SportId") for row in standing_rows} == {INDOOR}
        and resource.sport in {"", INDOOR}
    ):
        pool = importer.pool({"PoolId": resource.source_id}, INDOOR)
    if pool is not None and table is not None:
        importer.apply("pool_results", resource.source_id, data)
    meaningful = bool((table or {}).get("PoolStandingTeam"))
    resource.coverage = "partial" if meaningful else "empty"
    resource.reason = (
        "standings_without_dated_results" if meaningful else "no_dated_results"
    )
    resource.evidence = {
        "matches": 0,
        "official_table": table,
        "source_accounted": False,
    }


def refresh_app_pool_coverage(season: Season, identifiers: set[str]) -> None:
    """Refresh previously fetched tables when a detailed result changes their proof."""
    pools = dict(
        Pool.objects.filter(season=season, external_id__in=identifiers).values_list(
            "external_id", "results_filtered"
        )
    )
    for resource in HistoricalResource.objects.filter(
        season=season, provider="app", kind="pool", source_id__in=pools, state="fetched"
    ):
        resource.coverage, resource.evidence = pool_coverage(
            resource, {"ResultsFiltered": pools[resource.source_id]}
        )
        resource.reason = (
            "" if resource.coverage == "complete" else "standings_results_disagree"
        )
        resource.save(update_fields=("coverage", "evidence", "reason"))


def reuse_pool_matches(resource: HistoricalResource, identifiers: list[str]) -> None:
    """Checkpoint complete bulk results together without requesting their details."""
    prefix = "ds:" if resource.provider == "dataservice" else ""
    rows = Match.objects.filter(
        season=resource.season,
        external_id__in=identifiers,
        pool__external_id=prefix + resource.source_id,
    )
    if resource.provider == "app":
        # Dataservice details only enrich the poule link; app details can also
        # recover a missing final score, so leave those pending for the worker.
        rows = rows.filter(
            status="FINAL", home_score__isnull=False, away_score__isnull=False
        )
    matches = {
        match.external_id.removeprefix(prefix): match
        for match in rows.select_related("home_team")
    }
    ready = []
    for pending in HistoricalResource.objects.select_for_update().filter(
        season=resource.season,
        provider=resource.provider,
        kind="match",
        source_id__in=matches,
        state="pending",
        fetched_at__isnull=True,
    ):
        match = matches[pending.source_id]
        if (
            not pending.start_date
            <= timezone.localdate(match.starts_at)
            <= pending.end_date
        ):
            continue
        if pending.sport and pending.sport != match.home_team.sport:
            continue
        pending.state, pending.coverage = "fetched", "partial"
        pending.evidence = {"reused_match": match.pk}
        pending.fetched_at = timezone.now()
        pending.attempts, pending.reason = 0, ""
        ready.append(pending)
    HistoricalResource.objects.bulk_update(
        ready, ("state", "coverage", "evidence", "fetched_at", "attempts", "reason")
    )
    HistoricalDiscovery.objects.bulk_create(
        [
            HistoricalDiscovery(
                resource=pending, parent=resource, reference=f"resource/{resource.pk}"
            )
            for pending in ready
        ],
        ignore_conflicts=True,
    )


def pool_coverage(
    resource: HistoricalResource, data: dict[str, Any]
) -> tuple[str, dict]:
    """Require every team's played count to match dated, scored final fixtures."""
    pool = Pool.objects.get(season=resource.season, external_id=resource.source_id)
    rows = list(PoolEntry.objects.filter(pool=pool).select_related("team"))
    matches = list(
        Match.objects.filter(pool=pool).select_related("home_team", "away_team")
    )
    expected = {
        row.team.external_id: row.standing.get("TotalMatches")
        for row in rows
        if is_official_standing(row.standing)
    }
    complete, proof = fixture_coverage(
        (
            (
                match.home_team.external_id,
                match.away_team.external_id,
                match.status == "FINAL"
                and match.home_score is not None
                and match.away_score is not None,
            )
            for match in matches
        ),
        expected,
        members={row.team.external_id for row in rows},
        unfiltered=data.get("ResultsFiltered") is False,
        accounted=resource.evidence.get("source_accounted") is True,
    )
    return ("complete" if complete else "partial"), {
        **resource.evidence,
        **proof,
        "results_filtered": data.get("ResultsFiltered"),
    }


def progress() -> list[dict]:
    """Expose coverage separately from transport completion, grouped by season."""
    return list(
        HistoricalResource.objects
        .values(
            "season__name",
            "provider",
            "kind",
            "state",
            "coverage",
            "reason",
        )
        .annotate(resources=Count("pk"))
        .order_by("season__name", "provider", "kind", "state")
    )


__all__ = ["ARCHIVE_PREFIX"]
