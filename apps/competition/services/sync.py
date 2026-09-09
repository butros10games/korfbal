"""Paced, resumable GET-only discovery with ETags and bounded retries."""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
import time
import uuid

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.competition.application.ports import (
    AuthenticationRequiredError,
    CompetitionClient,
    FetchResult,
    ProviderCooldownError,
    RequestBudgetError,
    TransportError,
)
from apps.competition.models import Match, SyncLease, SyncResource
from apps.competition.services.importer import Importer, enqueue
from apps.competition.services.polling import (
    EXPECTED_DURATION,
    PollJob,
    PollPlanner,
    mark_checked,
    next_result_check,
)
from apps.competition.services.publishing import publish_catalogue
from apps.competition.services.resources import ENDPOINTS, MAX_FEED_FAILURES
from apps.competition.services.traffic import TrafficGate, observe_rate_limit
from apps.schedule.models import Season


HTTP_OK = 200
MAX_REQUESTS = 10000
LEASE_SECONDS = 120
HTTP_NOT_MODIFIED = 304
HTTP_RATE_LIMIT = 429
HTTP_FORBIDDEN = 403
AUTH_ERRORS = {401, 403}


class SyncUnavailableError(ValueError):
    """Another importer owns the provider lease, or a cooldown is active."""


def preview_sync(season: Season, *, budget: int | None = 100) -> dict[str, object]:
    """Count eligible feeds without HTTP, credentials, leases or checkpoint writes.

    This snapshot is conservative: overlapping successful responses can remove
    requests, while failures, OAuth and newly discovered resources are unknown.

    Raises:
        ValueError: The request budget is invalid.

    """
    if budget is not None and not 1 <= budget <= MAX_REQUESTS:
        raise ValueError("Request budget must be between 1 and 10000")
    planner = PollPlanner(season, timezone.now())
    counts: dict[str, int] = {}
    if ("clubs", "") not in planner.resources and not SyncResource.objects.filter(
        season=season, kind="clubs"
    ).exists():
        counts["clubs"] = 1
    for job in planner.candidate_jobs():
        kind = job.resource.kind
        counts[kind] = counts.get(kind, 0) + 1
    total = sum(counts.values())
    overdue = [
        next_result_check(row, planner.now)
        for row in planner.rows
        if row["starts_at"] + EXPECTED_DURATION <= planner.now
        and row["status"] not in {"CANCELLED", "WITHDRAWN", "POSTPONED"}
        and not (
            row["status"] == "FINAL"
            and row["home_score"] is not None
            and row["away_score"] is not None
        )
        and next_result_check(row, planner.now) <= planner.now
    ]
    return {
        "dry_run": True,
        "candidate_feed_requests": total,
        "batch_feed_requests_upper_bound": min(budget, total) if budget else total,
        "by_kind": counts,
        "overdue_pending_matches": len(overdue),
        "oldest_result_check_overdue_seconds": max(
            (int((planner.now - due).total_seconds()) for due in overdue),
            default=0,
        ),
        "max_http_requests": budget,
        "note": (
            "Local snapshot before response deduplication; excludes OAuth, retries "
            "and newly discovered feeds; assumes no failure fallback. "
            "Shared quotas/cooldowns may defer work."
        ),
    }


def checkpoint(resource: SyncResource, result: FetchResult, job: PollJob) -> bool:
    """Commit normalized data and the fetch checkpoint in the same transaction.

    Raises:
        ValueError: The response cannot be applied to this checkpoint.

    """
    now = timezone.now()
    with transaction.atomic():
        if result.status == HTTP_OK:
            if result.data is None:
                raise ValueError("Missing collection body")
            Importer(resource.season, now).apply(
                resource.kind, resource.source_id, result.data
            )
            resource.etag = result.etag
        elif result.status != HTTP_NOT_MODIFIED or resource.fetched_at is None:
            raise ValueError("Unexpected conditional response")
        resource.fetched_at = now
        resource.next_sync_at = now + timedelta(hours=ENDPOINTS[resource.kind][3])
        if resource.kind == "match_lineup":
            fixture = Match.objects.get(
                season=resource.season, external_id=resource.source_id
            )
            finish = fixture.starts_at + timedelta(hours=2)
            if finish > now:
                resource.next_sync_at = min(now + timedelta(days=1), finish)
        resource.failures = 0
        resource.last_error = ""
        resource.save()
        return mark_checked(job, now)


def record_failure(resource: SyncResource, code: str, delay: int = 60) -> None:
    """Back off a resource without persisting potentially sensitive exceptions."""
    resource.failures += 1
    delay = max(delay, min(60 * 2 ** min(resource.failures, 10), 86400))
    resource.next_sync_at = timezone.now() + timedelta(seconds=delay)
    resource.last_error = code
    resource.save(update_fields=("failures", "next_sync_at", "last_error"))


def sync(
    season: Season,
    client: CompetitionClient | None = None,
    *,
    client_factory: Callable[[], CompetitionClient] | None = None,
    budget: int | None = 100,
    max_seconds: int | None = None,
) -> dict[str, int]:
    """Drain a bounded slice of due work; repeat the command to resume discovery.

    Raises:
        ValueError: A budget is invalid.
        SyncUnavailableError: The provider lease is unavailable.

    """
    if budget is not None and not 1 <= budget <= MAX_REQUESTS:
        raise ValueError("Request budget must be between 1 and 10000")
    if max_seconds is not None and max_seconds <= 0:
        raise ValueError("Run duration must be positive")
    if (client is None) == (client_factory is None):
        raise ValueError("Provide exactly one client or client factory")
    started = time.monotonic()
    now = timezone.now()
    owner = uuid.uuid4()
    lease, _ = SyncLease.objects.get_or_create(
        key="sportlink", defaults={"expires_at": now}
    )
    claimed = SyncLease.objects.filter(pk=lease.pk, expires_at__lte=now).update(
        owner=owner, expires_at=now + timedelta(seconds=LEASE_SECONDS)
    )
    if not claimed:
        raise SyncUnavailableError(
            "Another import is running or provider cooldown is active"
        )
    summary = {
        "requests": 0,
        "updated": 0,
        "unchanged": 0,
        "failed": 0,
        "reauth_required": 0,
        "http_requests": 0,
        "deferred": 0,
        "matches_checked": 0,
    }
    cooldown = 0
    try:
        if client_factory is not None:
            client = client_factory()
        assert client is not None
        enqueue(season, "clubs")
        planner = PollPlanner(season, timezone.now())
        gate = TrafficGate(
            budget,
            owner,
            deadline=started + max_seconds if max_seconds else None,
        )
        cooldown = _drain(planner, client, gate, budget, summary)
        if summary["updated"]:
            publication = publish_catalogue(lease_owner=owner)
            summary["publication_blocked"] = len(publication["blocked"])
    finally:
        try:
            if client_factory is not None and client is not None:
                client.close()
        finally:
            SyncLease.objects.filter(pk=lease.pk, owner=owner).update(
                owner=None, expires_at=timezone.now() + timedelta(seconds=cooldown)
            )
    summary["pending"] = (
        SyncResource.objects
        .filter(season=season, failures__lt=MAX_FEED_FAILURES)
        .filter(Q(fetched_at__isnull=True) | Q(next_sync_at__lte=timezone.now()))
        .count()
    )
    summary["exhausted"] = SyncResource.objects.filter(
        season=season, failures__gte=MAX_FEED_FAILURES
    ).count()
    summary["elapsed_ms"] = round((time.monotonic() - started) * 1000)
    return summary


def _drain(
    planner: PollPlanner,
    client: CompetitionClient,
    gate: TrafficGate,
    budget: int | None,
    summary: dict[str, int],
) -> int:
    """Fetch due shared feeds until the snapshot, worker window or quota is spent."""
    cooldown = 0
    while budget is None or summary["requests"] < budget:
        job = planner.next_job()
        if job is None:
            break
        summary["requests"] += 1
        before_requests = gate.requests
        try:
            cooldown, checked = _fetch_one(job, client, summary, gate)
        except RequestBudgetError as exc:
            summary["deferred"] = 1
            cooldown = max(0, int((exc.retry_at - timezone.now()).total_seconds()) + 1)
            break
        finally:
            key = f"http_requests_{job.resource.kind}"
            summary[key] = summary.get(key, 0) + gate.requests - before_requests
        planner.completed(job, checked=checked)
        if cooldown:
            break
    if budget is not None and gate.requests >= budget and planner.candidate_jobs():
        summary["deferred"] = 1
    summary["http_requests"] = gate.requests
    summary["matches_checked"] = len(planner.checked)
    summary.update(planner.result_metrics())
    return cooldown


def _fetch_one(
    job: PollJob, client: CompetitionClient, summary: dict[str, int], gate: TrafficGate
) -> tuple[int, bool]:
    """Stop globally on authentication/rate limiting; isolate other feed failures."""
    resource = job.resource
    if resource.failures >= MAX_FEED_FAILURES:
        return 0, False
    checked = False
    try:
        result = client.fetch(resource, gate)
        if result.status == HTTP_RATE_LIMIT:
            observe_rate_limit()
        if result.status not in {HTTP_OK, HTTP_NOT_MODIFIED}:
            record_failure(resource, f"http_{result.status}", result.retry_after)
            summary["failed"] += 1
            if result.status == HTTP_RATE_LIMIT or (
                result.status in AUTH_ERRORS
                and resource.kind not in {"club_logo", "player_photo"}
                and not (
                    resource.kind in {"team_roster", "match_lineup"}
                    and result.status == HTTP_FORBIDDEN
                )
            ):
                return result.retry_after, False
            return 0, False
        checked = checkpoint(resource, result, job)
        summary["unchanged" if result.status == HTTP_NOT_MODIFIED else "updated"] += 1
    except AuthenticationRequiredError:
        record_failure(resource, "reauth_required")
        summary["failed"] += 1
        summary["reauth_required"] += 1
        return 60, False
    except TransportError as exc:
        if isinstance(exc, ProviderCooldownError):
            observe_rate_limit()
        delay = exc.seconds if isinstance(exc, ProviderCooldownError) else 60
        code = (
            "provider_cooldown"
            if isinstance(exc, ProviderCooldownError)
            else "transport_unavailable"
        )
        record_failure(resource, code, delay)
        summary["failed"] += 1
        return delay, False
    except (ValueError, KeyError, TypeError):
        record_failure(resource, "invalid_response_or_transport")
        summary["failed"] += 1
    return 0, checked
