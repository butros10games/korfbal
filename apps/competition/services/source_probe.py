"""Explicit bounded source reads that never import, publish or checkpoint data."""

from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import timedelta
from http import HTTPStatus
import re
import time
from typing import Any

from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.competition.application.ports import (
    AuthenticationRequiredError,
    FetchResult,
    HistoricalClient,
    ProviderCooldownError,
    RequestBudgetError,
    TransportError,
)
from apps.competition.models import HistoricalResource, SyncLease
from apps.competition.services.history import HistoryUnavailableError, validate_identity
from apps.competition.services.history_integrity import unique_matches
from apps.competition.services.history_sites import (
    CATALOGUE_VERSION,
    KORFBALNL,
    UITSLAGEN,
    korfbalnl_row,
    site_clubs,
    uitslagen_row,
)
from apps.competition.services.provider_scheduler import claim_lease
from apps.competition.services.seasons import INDOOR, OUTDOOR
from apps.competition.services.traffic import TrafficGate, observe_rate_limit
from apps.schedule.domain.competition_context import EDITION_FIRST_MONTH, edition_bounds
from apps.schedule.queries.seasons import season_edition


PROBE_KINDS = {
    "app": {"edition_team", "edition_pool"},
    KORFBALNL: {"catalogue", "club_matches"},
    UITSLAGEN: {"match_page"},
}
MAX_PROBE_REQUESTS = 100
MAX_PROBE_RESOURCES = 50
PROBE_DEADLINE_SECONDS = 300
SITE_SKIP_REASONS = {
    "not_played",
    "unsupported_sport",
    "club_unknown",
    "incomplete",
    "poule_unknown",
    "team_unknown",
}


def _object(value: object, *, optional: bool = True) -> dict[str, Any]:
    """Reject malformed nested envelopes without retaining their contents.

    Raises:
        TypeError: A required source envelope is not an object.

    """
    if optional and value is None:
        return {}
    if not isinstance(value, dict):
        raise TypeError("Expected a public source object")
    return value


def _objects(value: object, *, optional: bool = True) -> list[dict[str, Any]]:
    """Require object rows before reading or normalizing any provider fields.

    Raises:
        TypeError: The source collection or its rows have unsupported types.

    """
    if optional and value is None:
        return []
    if not isinstance(value, list):
        raise TypeError("Expected public fixture/table collections")
    if not all(isinstance(row, dict) for row in value):
        raise TypeError("Expected public fixture objects")
    return value


def _app_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Validate every nested object that the pure fixture validator accesses."""
    for row in rows:
        for side in ("HomeTeam", "AwayTeam"):
            team = _object(row.get(side), optional=False)
            _object(team.get("Club"), optional=False)
        for field in ("HomeResult", "AwayResult", "Pool"):
            _object(row.get(field))
    return rows


def _site_rows(rows: list[dict[str, Any]], provider: str) -> list[dict[str, Any]]:
    """Check the nested collections used by the existing public-site adapters."""
    for row in rows:
        if provider == UITSLAGEN:
            pool = _object(row.get("pool"))
            phase = _object(pool.get("phase"))
            _object(phase.get("sport"))
            _object(pool.get("division"))
            for side in ("home", "away"):
                team = _object(row.get(side))
                _object(team.get("club"))
        else:
            for field in ("status", "sport", "poule"):
                _object(row.get(field))
            for field in ("clubs", "teams", "stats"):
                sides = _object(row.get(field))
                for side in ("home", "away"):
                    _object(sides.get(side))
    return rows


def validate_probe(resource: HistoricalResource) -> None:
    """Reject unsupported/private endpoints and unbound editions before HTTP.

    Raises:
        ValueError: The resource is not a supported public historical probe.

    """
    validate_identity(resource.provider, resource.kind, resource.source_id)
    if resource.kind not in PROBE_KINDS.get(resource.provider, set()):
        raise ValueError("Select a public edition/team/pool or result-site resource")
    if season_edition(resource.season) is None:
        raise ValueError("The probe season needs an established edition")
    if resource.kind == "match_page" and not re.fullmatch(
        r"[0-9]+", resource.source_id
    ):
        raise ValueError("Use a numeric public-site page cursor")


def response_rows(
    resource: HistoricalResource, data: dict[str, Any]
) -> tuple[Sequence[dict[str, Any] | str], int, int]:
    """Normalize only supported public fixture envelopes, without importer calls.

    Returns:
        Fixture rows, observed standings count, and discovered pool count.

    Raises:
        ValueError: The response envelope or saved catalogue is unsupported.

    """
    data = _object(data, optional=False)
    if data.get("Error"):
        raise ValueError("The provider returned an application error")
    if resource.provider == "app":
        if resource.kind == "edition_pool":
            rows = _app_rows(_objects(data["MatchResult"], optional=False))
            table = _object(data.get("PoolStanding"))
            standings = _objects(table.get("PoolStandingTeam"))
            pools = []
        else:
            unbound = _object(data.get("UnboundMatchResults"))
            rows = _app_rows(_objects(unbound.get("MatchResult")))
            standings = []
            pools = _objects(data.get("Pool"))
        return rows, len(standings), len(pools)
    if resource.kind == "catalogue":
        for key in ("clubs", "sports", "poules"):
            _objects(data[key], optional=False)
        return [], 0, len(data["poules"])
    if resource.provider == UITSLAGEN:
        rows = _site_rows(_objects(data["rows"], optional=False), UITSLAGEN)
        return [uitslagen_row(row) for row in rows], 0, 0
    catalogue = HistoricalResource.objects.get(
        season=resource.season,
        provider=KORFBALNL,
        kind="catalogue",
    ).evidence
    catalogue = _object(catalogue, optional=False)
    if catalogue.get("version") != CATALOGUE_VERSION:
        raise ValueError("A supported existing public catalogue is required")
    for field in ("clubs", "sports", "pools"):
        _object(catalogue.get(field), optional=False)
    weeks = _objects(data["weeks"], optional=False)
    matches = _site_rows(
        [row for week in weeks for row in _objects(week["matches"], optional=False)],
        KORFBALNL,
    )
    clubs, _ = site_clubs(catalogue, matches)
    return [korfbalnl_row(row, catalogue, clubs) for row in matches], 0, 0


def summarize_response(resource: HistoricalResource, data: object) -> dict[str, Any]:
    """Return aggregate availability, date/sport routing and skip evidence only.

    No arbitrary response values or person-bearing payloads escape this boundary.
    Routing is a date/sport candidate count, not a promise to override pool period
    resolution or the existing app-over-site authority policy.

    Raises:
        ValueError: Skip codes or edition context are unsupported.

    """
    data = _object(data, optional=False)
    rows, standings, pools = response_rows(resource, data)
    if any(isinstance(row, str) and row not in SITE_SKIP_REASONS for row in rows):
        raise ValueError("Unsupported public-site skip reason")
    skipped = Counter(row for row in rows if isinstance(row, str))
    fixtures = unique_matches(row for row in rows if isinstance(row, dict))
    months: Counter[str] = Counter()
    sports: Counter[str] = Counter()
    routed: Counter[str] = Counter()
    statuses: Counter[str] = Counter()
    edition = season_edition(resource.season)
    if edition is None:
        raise ValueError("The probe season needs an established edition")
    first, last = edition_bounds(edition)
    for row in fixtures:
        status = row.get("Status")
        statuses[status if status in {"FINAL", "SUSPENDED"} else "other"] += 1
        stamp = parse_datetime(str(row.get("MatchDateTime") or ""))
        if stamp is None or timezone.is_naive(stamp):
            skipped["invalid_timestamp"] += 1
            continue
        day = timezone.localdate(stamp)
        months[f"{day.year:04d}-{day.month:02d}"] += 1
        side_sports = {row[side].get("SportId") for side in ("HomeTeam", "AwayTeam")}
        if len(side_sports) != 1:
            skipped["sport_mismatch"] += 1
            continue
        sport = side_sports.pop()
        sports[{INDOOR: "indoor", OUTDOOR: "outdoor"}.get(sport, "unsupported")] += 1
        if sport not in {INDOOR, OUTDOOR}:
            skipped["unsupported_sport"] += 1
        elif not first <= day <= last:
            skipped["outside_season_dates"] += 1
        elif day >= timezone.localdate():
            skipped["not_historical"] += 1
        elif row["HomeTeam"]["PublicTeamId"] == row["AwayTeam"]["PublicTeamId"]:
            skipped["self_match"] += 1
        else:
            routed[
                "indoor"
                if sport == INDOOR
                else "autumn"
                if day.month >= EDITION_FIRST_MONTH
                else "spring"
            ] += 1
    catalogue_rows = (
        sum(len(data[key]) for key in ("clubs", "sports", "poules"))
        if resource.kind == "catalogue"
        else 0
    )
    useful = bool(fixtures or standings or pools or catalogue_rows)
    return {
        "availability": "available"
        if useful
        else "all_rows_skipped"
        if rows
        else "empty",
        "returned_rows": len(rows),
        "unique_fixtures": len(fixtures),
        "standing_rows": standings,
        "standings_only": int(bool(standings) and not rows),
        "discovered_pools": pools,
        "catalogue_rows": catalogue_rows,
        "months": dict(months),
        "sports": dict(sports),
        "statuses": dict(statuses),
        "routing_candidates": dict(routed),
        "skipped": dict(skipped),
    }


@dataclass(frozen=True)
class ProbeOutcome:
    """Retain safe aggregate evidence and operational control state per resource."""

    attempted: bool = False
    availability: str = ""
    stop_status: str = ""
    cooldown: int = 0
    summary: dict[str, Any] | None = None


def _response_outcome(
    resource: HistoricalResource, response: FetchResult
) -> ProbeOutcome:
    """Classify HTTP status before inspecting any successful response fields."""
    if response.status in {HTTPStatus.NOT_FOUND, HTTPStatus.GONE}:
        return ProbeOutcome(attempted=True, availability="unavailable")
    if response.status == HTTPStatus.UNAUTHORIZED:
        return ProbeOutcome(
            attempted=True,
            availability="authentication_failed",
            stop_status="authentication_failed",
        )
    if response.status == HTTPStatus.TOO_MANY_REQUESTS:
        observe_rate_limit()
        return ProbeOutcome(
            attempted=True,
            availability="rate_limited",
            stop_status="rate_limited",
            cooldown=response.retry_after,
        )
    if response.status != HTTPStatus.OK:
        return ProbeOutcome(attempted=True, availability="provider_failed")
    summary = summarize_response(resource, response.data)
    return ProbeOutcome(
        attempted=True,
        availability=summary["availability"],
        summary=summary,
    )


def _attempt_probe(
    resource: HistoricalResource, client: HistoricalClient, gate: TrafficGate
) -> ProbeOutcome:
    """Continue malformed resources while honoring budget and backoff stops."""
    attempted = False
    try:
        response = client.fetch(resource, gate)
        attempted = True
        return _response_outcome(resource, response)
    except RequestBudgetError:
        return ProbeOutcome(stop_status="budget_deferred")
    except AuthenticationRequiredError:
        return ProbeOutcome(
            availability="authentication_failed",
            stop_status="authentication_failed",
        )
    except ProviderCooldownError as exc:
        observe_rate_limit()
        return ProbeOutcome(
            availability="cooldown_deferred",
            stop_status="cooldown_deferred",
            cooldown=exc.seconds,
        )
    except (TransportError, HistoryUnavailableError) as exc:
        return ProbeOutcome(
            availability="transport_failed"
            if isinstance(exc, TransportError)
            else "unavailable"
        )
    except (ValueError, KeyError, TypeError, HistoricalResource.DoesNotExist):
        return ProbeOutcome(attempted=attempted, availability="invalid")


def _accumulate_summary(
    result: dict[str, Any],
    aggregates: dict[str, Counter[str]],
    summary: dict[str, Any] | None,
) -> None:
    """Add only the fixed aggregate contract of a valid source response."""
    if summary is None:
        return
    for key, counter in aggregates.items():
        counter.update(summary[key])
    for key in (
        "returned_rows",
        "unique_fixtures",
        "standing_rows",
        "standings_only",
        "discovered_pools",
        "catalogue_rows",
    ):
        result[key] = result.get(key, 0) + summary[key]


def probe_sources(
    resources: Iterable[HistoricalResource],
    client_factory: Callable[[], HistoricalClient],
    *,
    budget: int,
) -> dict[str, Any]:
    """Claim existing traffic safety boundaries; never save imported evidence.

    Durable quota/lease reservations and possible OAuth rotation are the only
    writes. They must not be rolled back after sending actual network traffic.

    Raises:
        ValueError: The explicit resource selection or request budget is invalid.

    """
    selected = list(resources)
    if (
        not 1 <= budget <= MAX_PROBE_REQUESTS
        or not 1 <= len(selected) <= MAX_PROBE_RESOURCES
    ):
        raise ValueError(
            "Provide 1-50 resources and an explicit budget of 1-100 HTTP requests"
        )
    for resource in selected:
        validate_probe(resource)
    result: dict[str, Any] = {
        "http_requests": 0,
        "max_http_requests": budget,
        "domain_writes": 0,
        "publication_writes": 0,
        "checkpoint_writes": 0,
        "operational_writes": [],
        "attempted_resources": 0,
        "selected_resources": len(selected),
        "availability": {},
        "status": "completed",
        "coverage_certified_complete": False,
        "scope": (
            "Selected source responses only; no imports or source-priority changes."
        ),
    }
    owner = claim_lease()
    result["operational_writes"] = ["provider_lease"]
    if owner is None:
        return {**result, "status": "lease_busy"}
    gate = TrafficGate(
        budget, owner, deadline=time.monotonic() + PROBE_DEADLINE_SECONDS
    )
    client = None
    cooldown = 0
    availability: Counter[str] = Counter()
    aggregates: dict[str, Counter[str]] = {
        key: Counter()
        for key in (
            "months",
            "sports",
            "statuses",
            "routing_candidates",
            "skipped",
        )
    }
    try:
        client = client_factory()
        result["operational_writes"] = [
            "provider_lease",
            "provider_traffic",
            "oauth_session_if_refreshed",
        ]
        for resource in selected:
            outcome = _attempt_probe(resource, client, gate)
            result["attempted_resources"] += int(outcome.attempted)
            if outcome.availability:
                availability[outcome.availability] += 1
            _accumulate_summary(result, aggregates, outcome.summary)
            if outcome.stop_status:
                result["status"] = outcome.stop_status
                cooldown = outcome.cooldown
                break
    finally:
        if client is not None:
            client.close()
        SyncLease.objects.filter(key="sportlink", owner=owner).update(
            expires_at=timezone.now() + timedelta(seconds=cooldown),
        )
    return {
        **result,
        "http_requests": gate.requests,
        "availability": dict(availability),
        **{key: dict(counter) for key, counter in aggregates.items()},
    }
