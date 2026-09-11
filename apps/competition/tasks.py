"""Scheduled competition sync through the existing Celery runtime."""

import logging
from pathlib import Path

from celery import shared_task
from django.conf import settings
from django.utils import timezone

from apps.competition.application.ports import CompetitionClient
from apps.competition.composition import competition_client, run_match_form_queue
from apps.competition.models import MatchFormSync, SyncLease, SyncResource
from apps.competition.services.match_form_worker import discover
from apps.competition.services.monitoring import observe_run, outcome, progress
from apps.competition.services.resources import MAX_FEED_FAILURES
from apps.competition.services.sync import SyncUnavailableError, preview_sync, sync
from apps.schedule.models import Season


logger = logging.getLogger(__name__)


@shared_task(ignore_result=True, soft_time_limit=230, time_limit=240)
def sync_match_forms() -> str:
    """Drain durable, account-scoped form work including finished-match recovery."""
    result = run_match_form_queue()
    if (
        result != "busy"
        and MatchFormSync.objects.filter(
            state__in={"pending", "running"}, next_attempt_at__lte=timezone.now()
        ).exists()
    ):
        sync_match_forms.apply_async(expires=300)
    return result


@shared_task(ignore_result=True)
def discover_match_forms() -> None:
    """Discover timed imports and recover missed or abandoned queue dispatches."""
    discover()
    if MatchFormSync.objects.filter(
        state__in={"pending", "running"}, next_attempt_at__lte=timezone.now()
    ).exists():
        sync_match_forms.apply_async(expires=300)


class SessionUnavailableError(Exception):
    """The configured session cannot be loaded safely."""


def _scheduled_client() -> CompetitionClient:
    """Read the latest rotated credentials only after acquiring the provider lease.

    Raises:
        SessionUnavailableError: Credentials are missing, insecure or malformed.

    """
    try:
        return competition_client(
            session_file=Path(settings.SPORTLINK_SYNC_SESSION_FILE)
        )
    except (OSError, ValueError, TypeError):
        raise SessionUnavailableError from None


@shared_task(ignore_result=True)
def sync_current_competition() -> dict[str, object]:
    """Run one bounded batch using the shared provider pacing and lease."""
    if not settings.SPORTLINK_SYNC_ENABLED:
        return {"status": "disabled", "http_requests": 0}
    if not settings.SPORTLINK_SYNC_SEASON or not settings.SPORTLINK_SYNC_SESSION_FILE:
        logger.warning("Competition sync requires a season and private session file")
        return {"status": "configuration_required", "http_requests": 0}
    today = timezone.localdate()
    season = Season.objects.filter(
        name=settings.SPORTLINK_SYNC_SEASON,
        start_date__lte=today,
        end_date__gte=today,
    ).first()
    if season is None:
        logger.warning("Competition sync configured season is not active")
        return {"status": "inactive_season", "http_requests": 0}
    return observe_run(season, lambda: _run_scheduled(season))


def _run_scheduled(season: Season) -> dict[str, object]:
    """Avoid opening credentials or loading catalogue snapshots while idle/busy."""
    if MatchFormSync.objects.filter(
        state__in={"pending", "running"}, next_attempt_at__lte=timezone.now()
    ).exists():
        return {"status": "match_form_pending", "http_requests": 0}
    if SyncLease.objects.filter(
        key="sportlink", expires_at__gt=timezone.now()
    ).exists():
        return {"status": "busy_or_cooldown", "http_requests": 0}
    progress("planning")
    backlog = preview_sync(season, budget=settings.SPORTLINK_SYNC_MAX_REQUESTS or None)
    if not backlog["candidate_feed_requests"]:
        exhausted = SyncResource.objects.filter(
            season=season, failures__gte=MAX_FEED_FAILURES
        ).count()
        retrying = SyncResource.objects.filter(season=season, failures__gt=0).exists()
        return {
            "status": "exhausted" if exhausted else "retrying" if retrying else "idle",
            "http_requests": 0,
            "exhausted": exhausted,
            "backlog": backlog,
        }
    try:
        summary = sync(
            season,
            client_factory=_scheduled_client,
            budget=settings.SPORTLINK_SYNC_MAX_REQUESTS or None,
            max_seconds=settings.SPORTLINK_SYNC_MAX_SECONDS,
        )
    except SyncUnavailableError:
        return {"status": "busy_or_cooldown", "http_requests": 0}
    except SessionUnavailableError:
        logger.warning("Competition sync cannot load its private OAuth session")
        return {"status": "session_unavailable", "http_requests": 0}
    backlog = preview_sync(season, budget=settings.SPORTLINK_SYNC_MAX_REQUESTS or None)
    logger.info(
        "Competition sync summary: %s; remaining feed candidates: %s", summary, backlog
    )
    if summary["deferred"]:
        logger.warning(
            "Competition sync deferred work at a configured limit or run deadline"
        )
    if summary["reauth_required"]:
        logger.warning("Competition sync requires a renewed login session")
    return {"status": outcome(summary), **summary, "backlog": backlog}
