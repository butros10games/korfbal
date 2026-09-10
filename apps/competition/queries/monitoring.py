"""Bounded database reads for the competition operations dashboard."""

from collections import defaultdict
from datetime import date, datetime, time, timedelta
from operator import itemgetter
from typing import Any

from django.conf import settings
from django.db.models import Count, Q, QuerySet
from django.db.models.functions import TruncDate, TruncHour
from django.utils import timezone

from apps.competition.domain.timing import expected_finish
from apps.competition.models import (
    Match,
    SyncLease,
    SyncResource,
    SyncRun,
    TrafficState,
)
from apps.competition.services.polling import MATCH_FIELDS
from apps.competition.services.resources import MAX_FEED_FAILURES
from apps.schedule.models import Season


FINAL = Q(status="FINAL", home_score__isnull=False, away_score__isnull=False)
MAX_HISTORY = 3000
EXCLUDED = ("CANCELLED", "WITHDRAWN", "POSTPONED", "SUSPENDED")


def day_bounds(day: date) -> tuple[datetime, datetime]:
    """Use local calendar boundaries, including daylight-saving transitions."""
    tz = timezone.get_current_timezone()
    return (
        datetime.combine(day, time.min, tzinfo=tz),
        datetime.combine(day + timedelta(days=1), time.min, tzinfo=tz),
    )


def _match_day(season: Season, day: date, now: datetime) -> dict[str, Any]:
    start, end = day_bounds(day)
    matches = Match.objects.filter(season=season)
    daily = list(
        matches
        .annotate(day=TruncDate("starts_at"))
        .values("day")
        .annotate(
            total=Count("pk"),
            final=Count("pk", filter=FINAL),
            excluded=Count("pk", filter=Q(status__in=EXCLUDED)),
        )
        .order_by("day")
    )
    peak = max((row["total"] for row in daily), default=1)
    for row in daily:
        row["width"] = round(row["total"] / peak * 100, 2)
        row["completion"] = round(row["final"] / row["total"] * 100, 1)
    selected = matches.filter(starts_at__gte=start, starts_at__lt=end)
    totals = selected.aggregate(
        total=Count("pk"),
        final=Count("pk", filter=FINAL),
        excluded=Count("pk", filter=Q(status__in=EXCLUDED)),
    )
    totals["pending"] = totals["total"] - totals["final"] - totals["excluded"]
    hourly = list(
        selected
        .annotate(hour=TruncHour("starts_at"))
        .values("hour")
        .annotate(
            total=Count("pk"),
            final=Count("pk", filter=FINAL),
        )
        .order_by("hour")
    )
    hourly_peak = max((row["total"] for row in hourly), default=1)
    for row in hourly:
        row["width"] = round(row["total"] / hourly_peak * 100, 2)
    overdue = []
    # Scope the match snapshot to the selected date, not the whole import catalogue.
    for row in (
        selected
        .filter(starts_at__lt=now)
        .exclude(FINAL)
        .exclude(status__in=EXCLUDED)
        .values(*MATCH_FIELDS)
    ):
        finish = expected_finish(row)
        if finish <= now:
            overdue.append({
                "pk": row["id"],
                "starts_at": row["starts_at"],
                "status": row["status"],
                "checked_at": row["results_checked_at"],
                "minutes": int((now - finish).total_seconds() / 60),
            })
    overdue.sort(key=itemgetter("minutes"), reverse=True)
    return {
        "daily": daily,
        "hourly": hourly,
        "totals": totals,
        "overdue": overdue[:25],
        "overdue_count": len(overdue),
    }


def _run_history(runs: QuerySet[SyncRun], day: date) -> dict[str, Any]:
    start, end = day_bounds(day)
    # At most one normal tick every 30s; cap unusual duplicate delivery volume.
    history = list(
        runs.filter(started_at__gte=start, started_at__lt=end).order_by("-started_at")[
            : MAX_HISTORY + 1
        ]
    )
    truncated = len(history) > MAX_HISTORY
    history = history[:MAX_HISTORY]
    counters: dict[str, int] = defaultdict(int)
    buckets: dict[datetime, dict[str, Any]] = {}
    for run in reversed(history):
        stamp = timezone.localtime(run.started_at)
        stamp = stamp.replace(minute=stamp.minute // 30 * 30, second=0, microsecond=0)
        bucket = buckets.setdefault(
            stamp, {"time": stamp, "requests": 0, "overdue": None, "feeds": None}
        )
        for key in (
            "http_requests",
            "matches_checked",
            "failed",
            "deferred",
            "measured_final_results",
            "measured_delay_seconds_total",
            "unmeasured_final_results",
            "new_final_results",
        ):
            counters[key] += run.summary.get(key, 0)
        counters["measured_delay_seconds_max"] = max(
            counters["measured_delay_seconds_max"],
            run.summary.get("measured_delay_seconds_max", 0),
        )
        bucket["requests"] += run.summary.get("http_requests", 0)
        if run.backlog:
            bucket["overdue"] = run.backlog.get("overdue_pending_matches")
            bucket["feeds"] = run.backlog.get("candidate_feed_requests")
    timeline = list(buckets.values())
    max_backlog = max((row["overdue"] or 0 for row in timeline), default=0) or 1
    for row in timeline:
        row["width"] = round((row["overdue"] or 0) / max_backlog * 100, 2)
    measured = counters["measured_final_results"]
    delay_mean = (
        round(counters["measured_delay_seconds_total"] / measured / 60, 1)
        if measured
        else None
    )
    delay_max = (
        round(counters["measured_delay_seconds_max"] / 60, 1) if measured else None
    )
    return {
        "runs": history[:30],
        "timeline": timeline,
        "counters": dict(counters),
        "history_truncated": truncated,
        "history_incomplete": any(
            run.status in {"error", "running"} for run in history
        ),
        "delay_mean": delay_mean,
        "delay_max": delay_max,
    }


def _configuration_alerts(
    season: Season, latest: SyncRun | None, now: datetime
) -> list[str]:
    alerts = []
    if not settings.SPORTLINK_SYNC_ENABLED:
        alerts.append("Scheduled polling is disabled.")
    elif not settings.SPORTLINK_SYNC_SEASON or not settings.SPORTLINK_SYNC_SESSION_FILE:
        alerts.append("Scheduled polling configuration is incomplete.")
    elif season.name != settings.SPORTLINK_SYNC_SEASON:
        alerts.append("This season is not the configured automatic polling scope.")
    elif not season.start_date <= timezone.localdate() <= season.end_date:
        alerts.append(
            "The configured season is outside its active dates; polling will skip it."
        )
    elif latest is None:
        alerts.append(
            "No scheduler history yet. Monitoring starts after the updated worker runs."
        )
    elif latest.started_at < now - timedelta(minutes=5):
        alerts.append(
            "No scheduler heartbeat in the last five minutes. "
            "Check the worker and beat service."
        )
    return alerts


def _health_alerts(
    season: Season,
    now: datetime,
    latest: SyncRun | None,
    snapshot: SyncRun | None,
    exhausted: int,
) -> list[str]:
    alerts = _configuration_alerts(season, latest, now)
    if SyncRun.objects.filter(
        season=season,
        status="running",
        started_at__gte=now - timedelta(days=1),
        started_at__lt=now - timedelta(minutes=10),
    ).exists():
        alerts.append(
            "An unfinished run is over ten minutes old; "
            "the worker may have been interrupted."
        )
    if latest and (
        latest.status in {"error", "session_unavailable"}
        or latest.summary.get("reauth_required")
    ):
        alerts.append("The latest run failed or needs a renewed provider session.")
    if exhausted:
        alerts.append(
            f"{exhausted} feeds exhausted their retries "
            "and are excluded from automatic polling."
        )
    if (
        snapshot
        and snapshot.finished_at
        and snapshot.finished_at < now - timedelta(minutes=10)
    ):
        alerts.append(
            "The backlog snapshot is over ten minutes old; "
            "its counts may no longer be current."
        )
    return alerts


def monitoring_dashboard(season: Season, day: date) -> dict[str, Any]:
    """Read aggregates and saved telemetry; never call or replan provider traffic."""
    now = timezone.now()
    runs = SyncRun.objects.filter(season=season)
    latest = runs.order_by("-started_at").first()
    snapshot = runs.exclude(backlog={}).order_by("-started_at").first()
    resources = SyncResource.objects.filter(season=season)
    failures = list(
        resources
        .filter(failures__gt=0)
        .values("kind")
        .annotate(
            total=Count("pk"),
            exhausted=Count("pk", filter=Q(failures__gte=MAX_FEED_FAILURES)),
        )
        .order_by("kind")
    )
    exhausted = sum(row["exhausted"] for row in failures)
    traffic = TrafficState.objects.filter(key="sportlink").first()
    lease = SyncLease.objects.filter(key="sportlink", expires_at__gt=now).first()
    spacing = settings.SPORTLINK_REQUEST_SPACING
    return {
        **_match_day(season, day, now),
        **_run_history(runs, day),
        "alerts": _health_alerts(season, now, latest, snapshot, exhausted),
        "timezone": timezone.get_current_timezone_name(),
        "now": now,
        "day": day,
        "season": season,
        "latest": latest,
        "snapshot": snapshot,
        "failures": failures,
        "exhausted": exhausted,
        "lease": lease,
        "spacing": spacing,
        "capacity": 3600 // max(1, spacing),
        "hour_requests": traffic.hour_requests
        if traffic and traffic.hour_start + timedelta(hours=1) > now
        else 0,
        "day_requests": traffic.day_requests
        if traffic and traffic.day_start + timedelta(days=1) > now
        else 0,
        "traffic": traffic,
        "hour_limit": settings.SPORTLINK_HOURLY_LIMIT,
        "day_limit": settings.SPORTLINK_DAILY_LIMIT,
        "backfill_spacing": settings.SPORTLINK_BACKFILL_REQUEST_SPACING,
    }
