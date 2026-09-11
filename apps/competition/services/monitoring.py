"""Record sanitized scheduler telemetry without retaining provider payloads."""

from collections.abc import Callable, Mapping
from contextvars import ContextVar
from datetime import timedelta
from functools import cache
import hashlib
from pathlib import Path
from uuid import UUID

from django.db.models import Q
from django.utils import timezone

from apps.competition.models import SyncLease, SyncResource, SyncRun
from apps.schedule.models import Season


SUMMARY_FIELDS = (
    "requests",
    "updated",
    "unchanged",
    "pending",
    "exhausted",
    "http_requests",
    "matches_checked",
    "schedules_checked",
    "failed",
    "deferred",
    "reauth_required",
    "elapsed_ms",
    "request_spacing_seconds",
    "new_final_results",
    "measured_final_results",
    "measured_delay_seconds_total",
    "measured_delay_seconds_max",
    "unmeasured_final_results",
    "publication_blocked",
)
BACKLOG_FIELDS = (
    "candidate_feed_requests",
    "due_match_feed_estimate",
    "missing_provider_results",
    "due_results",
    "due_schedules",
    "overdue_pending_matches",
    "oldest_result_check_overdue_seconds",
)
RETENTION_DAYS = 30
STALE_RUN_AFTER = timedelta(minutes=10)
MAX_FAILURE_DETAILS = 20
_active_run: ContextVar[SyncRun | None] = ContextVar("competition_run", default=None)


@cache
def code_fingerprint() -> str:
    """Identify the actual importer code, including independently started backfills."""
    digest = hashlib.sha256()
    for path in sorted(Path(__file__).resolve().parents[1].rglob("*.py")):
        digest.update(
            str(path.relative_to(Path(__file__).resolve().parents[1])).encode()
        )
        digest.update(path.read_bytes())
    return digest.hexdigest()[:16]


def progress(
    stage: str | None,
    summary: dict[str, int] | None = None,
    *,
    resource: SyncResource | None = None,
    code: str = "",
    error: Exception | None = None,
) -> None:
    """Persist bounded diagnostics; never retain exception messages or payloads."""
    record = _active_run.get()
    if record is None:
        return
    if stage is not None:
        record.diagnostics["stage"] = stage
    if resource is not None:
        record.diagnostics["resource_id"] = resource.pk
        record.diagnostics["resource_kind"] = resource.kind
    if error is not None:
        record.diagnostics["exception_type"] = type(error).__name__
    if code:
        failure: dict[str, object] = {"code": code, "stage": stage}
        if resource is not None:
            failure.update(resource_id=resource.pk, resource_kind=resource.kind)
        if error is not None:
            failure["exception_type"] = type(error).__name__
        failures = record.diagnostics.setdefault("failures", [])
        failures.append(failure)
        record.diagnostics["failures"] = failures[-MAX_FAILURE_DETAILS:]
    if summary is not None:
        record.summary = _counters(summary, SUMMARY_FIELDS)
    record.heartbeat_at = timezone.now()
    SyncRun.objects.filter(pk=record.pk, status="running").update(
        diagnostics=record.diagnostics,
        summary=record.summary,
        heartbeat_at=record.heartbeat_at,
        lease_owner=record.lease_owner,
    )


def bind_run_lease(owner: UUID) -> None:
    """Associate this run with its lease so reconciliation cannot close live work."""
    if record := _active_run.get():
        record.lease_owner = owner
        progress("opening_client")


def reconcile_interrupted_runs() -> int:
    """Close stale records only when their provider lease is no longer alive."""
    now = timezone.now()
    live_owners = SyncLease.objects.filter(
        expires_at__gt=now,
        owner__isnull=False,
    ).values("owner")
    return (
        SyncRun.objects
        .filter(status="running", started_at__lt=now - STALE_RUN_AFTER)
        .filter(
            Q(heartbeat_at__isnull=True) | Q(heartbeat_at__lt=now - STALE_RUN_AFTER)
        )
        .exclude(lease_owner__in=live_owners)
        .update(status="interrupted", finished_at=now)
    )


def outcome(summary: Mapping[str, object]) -> str:
    """Distinguish terminal stalls, recoverable failures and successful batches."""
    if summary.get("reauth_required"):
        return "session_unavailable"
    if summary.get("exhausted"):
        return "exhausted"
    if summary.get("failed"):
        return "retrying"
    if summary.get("deferred"):
        return "deferred"
    return "completed"


def _counters(value: object, fields: tuple[str, ...]) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    return {
        key: value[key]
        for key in fields
        if isinstance(value.get(key), int) and value[key] >= 0
    }


def observe_run(
    season: Season, run: Callable[[], dict[str, object]]
) -> dict[str, object]:
    """Retain heartbeats, including interrupted runs, and re-raise worker errors."""
    now = timezone.now()
    expired = list(
        SyncRun.objects
        .filter(started_at__lt=now - timedelta(days=RETENTION_DAYS))
        .order_by("started_at")
        .values_list("pk", flat=True)[:1000]
    )
    if expired:
        SyncRun.objects.filter(pk__in=expired).delete()
    reconcile_interrupted_runs()
    record = SyncRun.objects.create(
        season=season,
        started_at=now,
        heartbeat_at=now,
        diagnostics={"code_fingerprint": code_fingerprint(), "stage": "starting"},
    )
    token = _active_run.set(record)
    try:
        result = run()
    except Exception as exc:
        progress(record.diagnostics["stage"], error=exc, code="worker_exception")
        SyncRun.objects.filter(pk=record.pk).update(
            finished_at=timezone.now(),
            status="error",
        )
        raise
    finally:
        _active_run.reset(token)
    allowed_statuses = {
        "completed",
        "idle",
        "busy_or_cooldown",
        "session_unavailable",
        "retrying",
        "exhausted",
        "deferred",
    }
    run_status = result.get("status")
    SyncRun.objects.filter(pk=record.pk).update(
        finished_at=timezone.now(),
        status=run_status if run_status in allowed_statuses else "error",
        summary=_counters(result, SUMMARY_FIELDS),
        backlog=_counters(result.get("backlog"), BACKLOG_FIELDS),
    )
    return result
