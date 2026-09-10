"""Record sanitized scheduler telemetry without retaining provider payloads."""

from collections.abc import Callable
from datetime import timedelta

from django.utils import timezone

from apps.competition.models import SyncRun
from apps.schedule.models import Season


SUMMARY_FIELDS = (
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
    "due_results",
    "due_schedules",
    "overdue_pending_matches",
    "oldest_result_check_overdue_seconds",
)
RETENTION_DAYS = 30


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
    record = SyncRun.objects.create(season=season, started_at=now)
    try:
        result = run()
    except Exception:
        SyncRun.objects.filter(pk=record.pk).update(
            finished_at=timezone.now(),
            status="error",
        )
        raise
    allowed_statuses = {"completed", "idle", "busy_or_cooldown", "session_unavailable"}
    run_status = result.get("status")
    SyncRun.objects.filter(pk=record.pk).update(
        finished_at=timezone.now(),
        status=run_status if run_status in allowed_statuses else "error",
        summary=_counters(result, SUMMARY_FIELDS),
        backlog=_counters(result.get("backlog"), BACKLOG_FIELDS),
    )
    return result
