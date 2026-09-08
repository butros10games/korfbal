"""Paced, resumable GET-only discovery with ETags and bounded retries."""

from __future__ import annotations

from datetime import timedelta
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
from apps.competition.services.polling import PollJob, PollPlanner, mark_checked
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
    season: Season, client: CompetitionClient, *, budget: int = 100
) -> dict[str, int]:
    """Drain a bounded slice of due work; repeat the command to resume discovery.

    Raises:
        ValueError: The budget is invalid or the provider lease is unavailable.

    """
    if not 1 <= budget <= MAX_REQUESTS:
        raise ValueError("Request budget must be between 1 and 10000")
    now = timezone.now()
    owner = uuid.uuid4()
    lease, _ = SyncLease.objects.get_or_create(
        key="sportlink", defaults={"expires_at": now}
    )
    claimed = SyncLease.objects.filter(pk=lease.pk, expires_at__lte=now).update(
        owner=owner, expires_at=now + timedelta(seconds=LEASE_SECONDS)
    )
    if not claimed:
        raise ValueError("Another import is running or provider cooldown is active")
    summary = {
        "requests": 0,
        "updated": 0,
        "unchanged": 0,
        "failed": 0,
        "reauth_required": 0,
        "http_requests": 0,
        "deferred": 0,
    }
    cooldown = 0
    try:
        enqueue(season, "clubs")
        planner = PollPlanner(season, timezone.now())
        gate = TrafficGate(budget, owner)
        for _ in range(budget):
            job = planner.next_job()
            if job is None:
                break
            summary["requests"] += 1
            try:
                cooldown, checked = _fetch_one(job, client, summary, gate)
            except RequestBudgetError as exc:
                summary["deferred"] = 1
                cooldown = max(
                    0, int((exc.retry_at - timezone.now()).total_seconds()) + 1
                )
                break
            planner.completed(job, checked=checked)
            if cooldown:
                break
        summary["http_requests"] = gate.requests
        if summary["updated"]:
            publication = publish_catalogue(lease_owner=owner)
            summary["publication_blocked"] = len(publication["blocked"])
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
    return summary


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
