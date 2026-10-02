"""Bounded history batches yield to current results and share the provider lease."""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from http import HTTPStatus
import time
from typing import Any
import uuid

from django.conf import settings
from django.db import OperationalError, transaction
from django.db.models import Case, IntegerField, QuerySet, When
from django.utils import timezone

from apps.competition.application.ports import (
    AuthenticationRequiredError,
    HistoricalClient,
    ProviderCooldownError,
    RequestBudgetError,
    TransportError,
)
from apps.competition.models import (
    HistoricalResource,
    Match,
    MatchFormSync,
    SyncLease,
    SyncResource,
)
from apps.competition.services.history import (
    KINDS,
    HistoryUnavailableError,
    discover,
    split_window,
)
from apps.competition.services.history_checkpoint import checkpoint
from apps.competition.services.history_dataservice import reconcile_pool_coverage
from apps.competition.services.lineup_plan import settle
from apps.competition.services.polling import PollPlanner
from apps.competition.services.publication_worker import lock_conflict
from apps.competition.services.publishing import (
    MatchBounds,
    pending_matches,
    publish_catalogue,
)
from apps.competition.services.resources import MAX_FEED_FAILURES
from apps.competition.services.schedule_notifications import ScheduleChangeDispatcher
from apps.competition.services.traffic import TrafficGate, observe_rate_limit
from apps.schedule.models import Season


MAX_BUDGET = 1000
# Matches published per history turn, and the unpublished backlog at which a
# turn stops fetching and only publishes; publication must keep pace with imports.
PUBLISH_MATCHES = 1500
PUBLISH_BACKLOG = 3000
MAX_WINDOW_DAYS = 240
RESULT_PRIORITY_CHECK_SECONDS = 30
AUTH_REASONS = {
    "reauth_required",
    "access_denied",
    "app_session_required",
    "dataservice_credentials_required",
}


def live_work_waiting() -> bool:
    """Tell whether the live sync has any due feed, routine refreshes included."""
    today = timezone.localdate()
    return SyncResource.objects.filter(
        season__start_date__lte=today,
        season__end_date__gte=today,
        failures__lt=MAX_FEED_FAILURES,
        next_sync_at__lte=timezone.now(),
    ).exists()


def current_work_due(*, include_results: bool = True) -> bool:
    """Backfills yield to match forms, new discovery and time-critical checks.

    Periodic refreshes (rosters, photos, programmes, standings) form a continuous
    backlog in an active season; yielding to them would starve history, so
    history shares the provider lease with them instead.
    """
    now = timezone.now()
    if MatchFormSync.objects.filter(
        state__in={"pending", "running"}, next_attempt_at__lte=now
    ).exists():
        return True
    today = timezone.localdate(now)
    resources = SyncResource.objects.filter(
        season__start_date__lte=today,
        season__end_date__gte=today,
        failures__lt=MAX_FEED_FAILURES,
    )
    if resources.filter(fetched_at__isnull=True, next_sync_at__lte=now).exists():
        return True
    if not include_results:
        return False
    return any(
        PollPlanner(season, now).urgent_due()
        for season in Season.objects.filter(pk__in=resources.values("season_id"))
    )


def next_resource() -> HistoricalResource | None:
    """Prefer bulk poule discovery over per-match enrichment, newest scope first.

    Lineups wait until no other historical work is ready, so every edition's
    matches are imported before any lineup request is sent.
    """
    pending = HistoricalResource.objects.filter(state="pending")
    ready = pending.filter(next_attempt_at__lte=timezone.now()).select_related("season")
    found = discovery_resource(ready)
    if found is not None or pending.filter(kind__in=DISCOVERY_KINDS).exists():
        # A discovery checkpoint waiting for its retry still holds lineups back.
        return found
    return ready.filter(kind="lineup").order_by("next_attempt_at", "pk").first()


def discovery_resource(ready: QuerySet) -> HistoricalResource | None:
    """Return the most urgent ready checkpoint other than a lineup."""
    return (
        ready
        .filter(kind__in=DISCOVERY_KINDS)
        .annotate(
            priority=Case(
                When(kind="pool", then=0),
                When(kind="edition_pool", then=0),
                When(kind="pool_window", then=1),
                When(kind="standing", then=2),
                When(kind="members", then=2),
                When(kind="match", then=3),
                default=4,
                output_field=IntegerField(),
            ),
        )
        .order_by("-season__end_date", "priority", "-end_date", "pk")
        .first()
    )


@transaction.atomic
def local_work(resource: HistoricalResource, *, owner: uuid.UUID | None = None) -> bool:
    """Split oversized windows or reuse bulk results before spending network budget."""
    if owner is not None:
        lease = SyncLease.objects.select_for_update().get(key="sportlink", owner=owner)
        lease.expires_at = timezone.now() + timedelta(seconds=120)
        lease.save(update_fields=("expires_at",))
    if (
        resource.kind in {"window", "pool_window"}
        and (resource.end_date - resource.start_date).days > MAX_WINDOW_DAYS
    ):
        split_window(resource)
        resource.fetched_at, resource.etag = timezone.now(), ""
        resource.save()
        return True
    if resource.kind != "match" or resource.fetched_at:
        return False
    prefix = "ds:" if resource.provider == "dataservice" else ""
    existing = (
        Match.objects
        .filter(
            season=resource.season,
            starts_at__date__gte=resource.start_date,
            starts_at__date__lte=resource.end_date,
            external_id=prefix + resource.source_id,
            pool__isnull=False,
        )
        .select_related("pool")
        .first()
    )
    if existing is None:
        return False
    # App details can supply a missing result. The verified Dataservice detail
    # enrichment is the poule link; ambiguous scores cannot establish a final.
    if resource.provider == "app" and (
        existing.status != "FINAL"
        or existing.home_score is None
        or existing.away_score is None
    ):
        return False
    if resource.sport and existing.pool.sport != resource.sport:
        return False
    discover(resource, "pool", existing.pool.external_id.removeprefix(prefix))
    resource.state, resource.coverage = "fetched", "partial"
    resource.evidence = {"reused_match": existing.pk}
    resource.fetched_at = timezone.now()
    resource.attempts, resource.reason = 0, ""
    resource.save()
    return True


def fetch_resource(
    resource: HistoricalResource, client: HistoricalClient, gate: TrafficGate
) -> None:
    """Handle transport status before atomically applying historical evidence.

    Raises:
        ProviderCooldownError: The provider returned HTTP 429.
        HistoryUnavailableError: Access or historical identity is unavailable.
        TransportError: A temporary or unexpected HTTP response was received.

    """
    result = client.fetch(resource, gate)
    if result.status == HTTPStatus.TOO_MANY_REQUESTS:
        raise ProviderCooldownError(result.retry_after)
    if result.status in {HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN}:
        raise HistoryUnavailableError(
            "reauth_required"
            if result.status == HTTPStatus.UNAUTHORIZED
            else "access_denied"
        )
    if result.status in {HTTPStatus.NOT_FOUND, HTTPStatus.GONE}:
        raise HistoryUnavailableError("historical_resource_unavailable")
    if result.status == MATCH_NOT_FOUND and resource.kind == "lineup":
        # KNKV no longer serves details for some old matches; retrying cannot help.
        raise HistoryUnavailableError("lineup_unavailable")
    if result.status == HTTPStatus.NOT_MODIFIED and resource.fetched_at:
        with transaction.atomic():
            lease = SyncLease.objects.select_for_update().get(
                key="sportlink", owner=gate.owner
            )
            resource.state, resource.attempts = "fetched", 0
            resource.fetched_at = timezone.now()
            resource.save(update_fields=("state", "attempts", "fetched_at"))
            lease.expires_at = timezone.now() + timedelta(seconds=120)
            lease.save(update_fields=("expires_at",))
    elif result.status == HTTPStatus.OK and result.data is not None:
        resource.etag = result.etag
        checkpoint(resource, result.data, owner=gate.owner)
    else:
        raise TransportError("Historical HTTP failure")


# A checkpoint rolled back by a lock conflict waits this long before its retry.
LOCK_RETRY = timedelta(seconds=30)


# Every checkpoint kind except lineups, which wait for all of them.
DISCOVERY_KINDS = sorted(KINDS - {"lineup"})

# KNKV's validation error, e.g. MATCH_NOT_FOUND for details it no longer serves.
MATCH_NOT_FOUND = 420


class HistoryBatch:
    """Isolate per-resource retries from provider-wide cooldowns."""

    def __init__(self, client: HistoricalClient, gate: TrafficGate) -> None:
        """Bind the exclusively owned client and its wire request budget."""
        self.client, self.gate = client, gate
        self.cooldown = 0
        # Why process() stopped the batch: budget, cooldown (429), auth (the app
        # session renewal failed) or unavailable (this historical source only).
        self.stop = ""
        self.publish_deadline: float | None = None
        self.touched: set[int] = set()
        self.summary: dict[str, Any] = {
            "http_requests": 0,
            "fetched": 0,
            "blocked": 0,
            "failed": 0,
            "deferred": 0,
        }

    def process(self, resource: HistoricalResource) -> bool:
        """Attempt one checkpoint, then replan its lineup cohort.

        Returns:
            Whether this batch can continue after a single resource attempt.

        """
        self.touched.add(resource.pk)
        result = self.attempt(resource)
        settle(resource)
        return result

    def attempt(self, resource: HistoricalResource) -> bool:
        """Fetch one checkpoint, classifying provider and database failures.

        Returns:
            Whether this batch can continue.

        Raises:
            OperationalError: A database error other than a lock conflict.

        """
        try:
            if not local_work(resource, owner=self.gate.owner):
                fetch_resource(resource, self.client, self.gate)
            self.summary["fetched"] += 1
        except RequestBudgetError as exc:
            self.cooldown = max(
                0, int((exc.retry_at - timezone.now()).total_seconds()) + 1
            )
            self.summary["deferred"] += 1
            self.stop = "budget"
            return False
        except ProviderCooldownError as exc:
            self.cooldown = exc.seconds
            self.stop = "cooldown"
            with transaction.atomic():
                SyncLease.objects.select_for_update().get(
                    key="sportlink", owner=self.gate.owner
                )
                observe_rate_limit()
                resource.refresh_from_db()
                resource.reason = "http_429"
                resource.next_attempt_at = timezone.now() + timedelta(
                    seconds=exc.seconds
                )
                resource.save()
            self.summary["deferred"] += 1
            return False
        except AuthenticationRequiredError:
            self.block(resource, "reauth_required")
            self.summary["reason"] = "reauth_required"
            self.stop = "auth"
            return False
        except HistoryUnavailableError as exc:
            self.block(resource, str(exc))
            if str(exc) in AUTH_REASONS:
                # Access to this historical source only, not the app session.
                self.stop = "unavailable"
                return False
        except (TransportError, ValueError, KeyError, TypeError):
            self.record_failure(resource)
        except OperationalError as exc:
            if not lock_conflict(exc):
                raise
            # Publication won a row lock: the checkpoint rolled back, so retry it
            # shortly instead of failing the whole provider turn.
            HistoricalResource.objects.filter(pk=resource.pk).update(
                next_attempt_at=timezone.now() + LOCK_RETRY
            )
            self.summary["deferred"] += 1
        return True

    @transaction.atomic
    def block(self, resource: HistoricalResource, reason: str) -> None:
        """Require an explicit retry after rejected access or unavailable identity."""
        SyncLease.objects.select_for_update().get(
            key="sportlink", owner=self.gate.owner
        )
        resource.refresh_from_db()
        resource.state, resource.coverage, resource.reason = (
            "blocked",
            "inaccessible",
            reason,
        )
        resource.save()
        self.summary["blocked"] += 1

    @transaction.atomic
    def record_failure(self, resource: HistoricalResource) -> None:
        """Back off only while the failed request still owns its checkpoint."""
        SyncLease.objects.select_for_update().get(
            key="sportlink", owner=self.gate.owner
        )
        resource.refresh_from_db()
        resource.attempts += 1
        resource.reason = "invalid_response_or_transport"
        resource.next_attempt_at = timezone.now() + timedelta(
            seconds=min(60 * 2 ** min(resource.attempts, 11), 86400)
        )
        if resource.attempts >= MAX_FEED_FAILURES:
            resource.state = "failed"
        resource.save()
        self.summary["failed"] += 1

    def drain(
        self, *, publish_with: ScheduleChangeDispatcher | None, owner: uuid.UUID
    ) -> None:
        """Limit CPU-only discoveries too; publication runs once after the batch.

        ``publish_with`` is None when the operator explicitly skips publication.
        """
        budget = self.gate.budget
        assert (
            budget is not None
        )  # Historical imports always have an explicit batch cap.
        # Fetch only while publication keeps up; otherwise this turn publishes.
        backlog = pending_matches().count()
        if backlog >= PUBLISH_BACKLOG:
            budget = 0
        self.summary["unpublished"] = backlog
        next_result_check = time.monotonic() + RESULT_PRIORITY_CHECK_SECONDS
        for _ in range(budget * 4):
            include_results = time.monotonic() >= next_result_check
            if self.gate.requests >= budget or current_work_due(
                include_results=include_results
            ):
                break
            if include_results:
                next_result_check = time.monotonic() + RESULT_PRIORITY_CHECK_SECONDS
            resource = next_resource()
            if resource is None or not self.process(resource):
                break
        self.publish(publish_with=publish_with, owner=owner, backlog=backlog)

    def publish(
        self,
        *,
        publish_with: ScheduleChangeDispatcher | None,
        owner: uuid.UUID,
        backlog: int,
    ) -> None:
        """Reconcile touched poules and publish one bounded chunk of history."""
        with transaction.atomic():
            lease = SyncLease.objects.select_for_update().get(
                key="sportlink", owner=owner
            )
            reconcile_pool_coverage(self.touched)
            if publish_with is not None and (self.summary["fetched"] or backlog):
                result = publish_catalogue(
                    schedule_changes=publish_with,
                    lease_owner=owner,
                    bounds=MatchBounds(
                        limit=PUBLISH_MATCHES, deadline=self.publish_deadline
                    ),
                )
                self.summary["publication"] = result["counts"]
                self.summary["publication_conflicts"] = len(result["blocked"])
                self.summary["unpublished"] = pending_matches().count()
            lease.expires_at = timezone.now() + timedelta(seconds=120)
            lease.save(update_fields=("expires_at",))


def history_spacing() -> int:
    """Pace history separately: it is large, but never time-critical."""
    return min(
        settings.SPORTLINK_REQUEST_SPACING, settings.SPORTLINK_HISTORY_REQUEST_SPACING
    )


def run_history(
    client_factory: Callable[[], HistoricalClient],
    *,
    publish_with: ScheduleChangeDispatcher | None,
    budget: int = 20,
    deadline: float | None = None,
    publish_deadline: float | None = None,
) -> dict:
    """Resume a bounded slice of historical work under the shared provider lease.

    Raises:
        ValueError: The request budget is outside the supported range.

    """
    if not 1 <= budget <= MAX_BUDGET:
        raise ValueError("Historical request budget must be between 1 and 1000")
    if current_work_due():
        return {"http_requests": 0, "deferred": 1, "reason": "current_work_due"}
    now, owner = timezone.now(), uuid.uuid4()
    lease, _ = SyncLease.objects.get_or_create(
        key="sportlink", defaults={"expires_at": now}
    )
    if not SyncLease.objects.filter(pk=lease.pk, expires_at__lte=now).update(
        owner=owner, expires_at=now + timedelta(seconds=120)
    ):
        return {"http_requests": 0, "deferred": 1, "reason": "provider_lease_busy"}
    client, batch = None, None
    try:
        client = client_factory()
        gate = TrafficGate(budget, owner, deadline=deadline, spacing=history_spacing())
        batch = HistoryBatch(client, gate)
        batch.publish_deadline = publish_deadline
        batch.drain(publish_with=publish_with, owner=owner)
        batch.summary["http_requests"] = batch.gate.requests
        return batch.summary
    finally:
        try:
            if client:
                client.close()
        finally:
            SyncLease.objects.filter(pk=lease.pk, owner=owner).update(
                owner=None,
                expires_at=timezone.now()
                + timedelta(seconds=batch.cooldown if batch else 0),
            )
