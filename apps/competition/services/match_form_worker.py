"""Recoverable form queue sharing the catalogue worker's OAuth/traffic lease."""

from collections.abc import Callable
from datetime import timedelta
import time
from uuid import uuid4

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.competition.application.match_forms import (
    MatchFormError,
    MatchFormOptions,
    MatchFormProvider,
)
from apps.competition.application.ports import (
    AuthenticationRequiredError,
    ProviderCooldownError,
    RequestBudgetError,
    TransportError,
)
from apps.competition.models import (
    Match as SourceMatch,
    MatchFormAccess,
    MatchFormSync,
    SyncLease,
)
from apps.competition.services.match_forms import (
    enqueue,
    execute,
    import_is_due,
    import_slots,
)
from apps.competition.services.traffic import TrafficGate
from apps.game_tracker.application.ports import MatchChangePublisher
from apps.game_tracker.services.match_mutations import MatchRevisionConflictError


MAX_ATTEMPTS = 5


def discover() -> None:
    """Queue upcoming roster imports and opted-in recent finished-match corrections."""
    now = timezone.now()
    upcoming = Q(
        starts_at__gt=now,
        starts_at__lte=now + timedelta(hours=1),
        local_match__tracker_data__status="upcoming",
    )
    finished = Q(
        starts_at__gte=now - timedelta(days=2),
        pool__competition_class__category="a",
        local_match__tracker_data__status="finished",
    )
    for access in MatchFormAccess.objects.filter(enabled=True).select_related(
        "user", "team"
    ):
        sources = list(
            SourceMatch.objects.filter(
                Q(local_match__home_team=access.team)
                | Q(local_match__away_team=access.team),
                upcoming | finished if access.auto_substitutions else upcoming,
            ).values_list(
                "local_match_id",
                "starts_at",
                "local_match__tracker_data__status",
                "local_match__tracker_data__live_revision",
            )
        )
        if not sources:
            continue
        jobs = {
            (job.match_id, job.action): job
            for job in MatchFormSync.objects.filter(
                access=access,
                match_id__in=[row[0] for row in sources],
                action__in={"import", "substitutions"},
            ).only(
                "match_id",
                "action",
                "state",
                "updated_at",
                "player_count",
                "expected_revision",
            )
        }
        for match_id, starts_at, status, revision in sources:
            action = "import" if status == "upcoming" else "substitutions"
            job = jobs.get((match_id, action))
            if action == "import" and not import_is_due(starts_at, now, job):
                continue
            if (
                action == "substitutions"
                and job
                and (
                    job.state in {"pending", "running"}
                    or job.expected_revision == revision
                )
            ):
                continue
            try:
                enqueue(
                    access,
                    match_id,
                    action,
                    revision,
                    options=MatchFormOptions(automatic=True),
                )
            except (MatchFormError, MatchRevisionConflictError):
                continue


def drain(
    provider_factory: Callable[[TrafficGate], MatchFormProvider],
    publisher: MatchChangePublisher,
) -> str:
    """Run one job, restoring abandoned work before a later periodic invocation."""
    discover()
    now = timezone.now()
    owner = uuid4()
    lease, _ = SyncLease.objects.get_or_create(
        key="sportlink", defaults={"expires_at": now}
    )
    if not MatchFormSync.objects.filter(
        state__in={"pending", "running"}, next_attempt_at__lte=now
    ).exists():
        return "idle"
    if not SyncLease.objects.filter(pk=lease.pk, expires_at__lte=now).update(
        owner=owner, expires_at=now + timedelta(seconds=120)
    ):
        return "busy"
    cooldown = 0
    job = None
    try:
        with transaction.atomic():
            job = (
                MatchFormSync.objects
                .select_for_update()
                .filter(state__in={"pending", "running"}, next_attempt_at__lte=now)
                .order_by("next_attempt_at", "pk")
                .first()
            )
            if job is None:
                return "idle"
            job.state = "running"
            job.attempts += 1
            job.next_attempt_at = now + timedelta(minutes=5)
            if job.automatic and job.action == "import":
                start = SourceMatch.objects.values_list("starts_at", flat=True).get(
                    local_match_id=job.match_id
                )
                job.next_attempt_at = next(
                    (slot for slot in import_slots(start) if slot > now), start
                )
            job.updated_at = now
            job.save()
        gate = TrafficGate(None, owner, deadline=time.monotonic() + 200)
        provider = provider_factory(gate)
        execute(job, provider, publisher)
        job.state, job.error_code = "succeeded", ""
    except MatchRevisionConflictError:
        if job:
            job.state, job.error_code = "failed", "revision_conflict"
    except (
        MatchFormError,
        AuthenticationRequiredError,
        TransportError,
        ProviderCooldownError,
        RequestBudgetError,
        OSError,
        ValueError,
        TypeError,
    ) as exc:
        cooldown = _record_failure(job, exc)
    finally:
        if job:
            job.updated_at = timezone.now()
            job.save()
        SyncLease.objects.filter(pk=lease.pk, owner=owner).update(
            owner=None, expires_at=timezone.now() + timedelta(seconds=cooldown)
        )
    return job.state if job else "idle"


def _record_failure(job: MatchFormSync | None, exc: Exception) -> int:
    cooldown = exc.seconds if isinstance(exc, ProviderCooldownError) else 0
    if job:
        code = (
            exc.code
            if isinstance(exc, MatchFormError)
            else "session_unavailable"
            if isinstance(
                exc, (OSError, ValueError, TypeError, AuthenticationRequiredError)
            )
            else "knkv_unavailable"
        )
        retryable = code in {"connection_failed", "knkv_unavailable"} and not (
            job.automatic and job.action == "import"
        )
        job.state = "pending" if retryable and job.attempts < MAX_ATTEMPTS else "failed"
        job.error_code = code
        job.next_attempt_at = timezone.now() + timedelta(
            seconds=max(cooldown, 30 * job.attempts)
        )
        if isinstance(exc, RequestBudgetError):
            job.next_attempt_at = max(job.next_attempt_at, exc.retry_at)
    return cooldown
