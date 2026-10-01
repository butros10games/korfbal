"""Scheduled competition sync through the existing Celery runtime."""

import logging
from pathlib import Path
import time

from celery import shared_task
from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from apps.competition.application.ports import CompetitionClient, HistoricalClient
from apps.competition.composition import (
    competition_client,
    provider_clients,
    run_match_form_queue,
    schedule_change_dispatcher,
    scheduled_history_client,
)
from apps.competition.models import (
    HistoricalResource,
    MatchFormSync,
    SyncLease,
    SyncResource,
)
from apps.competition.services.history_editions import (
    current_edition,
    recheck_edition,
)
from apps.competition.services.history_worker import live_work_waiting, run_history
from apps.competition.services.match_form_worker import discover
from apps.competition.services.monitoring import observe_run, outcome, progress
from apps.competition.services.provider_scheduler import (
    ProviderTurn,
    TurnOptions,
    active_live_season,
    history_pending,
)
from apps.competition.services.publication_worker import (
    claim_publication,
    publish_backlog,
    release_publication,
)
from apps.competition.services.resources import MAX_FEED_FAILURES
from apps.competition.services.sync import (
    SyncUnavailableError,
    match_forms_due,
    preview_sync,
    sync_leased,
)
from apps.schedule.models import Season


logger = logging.getLogger(__name__)


@shared_task(ignore_result=True, soft_time_limit=230, time_limit=240)
def sync_match_forms() -> str:
    """Drain durable, account-scoped form work including finished-match recovery."""
    result = run_match_form_queue()
    if MatchFormSync.objects.filter(
        state__in={"pending", "running"}, next_attempt_at__lte=timezone.now()
    ).exists():
        sync_match_forms.apply_async(
            countdown=5 if result == "busy" else 0, expires=300
        )
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
    if settings.SPORTLINK_SCHEDULER != "legacy":
        return {
            "status": f"scheduler_{settings.SPORTLINK_SCHEDULER}",
            "http_requests": 0,
        }
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
        summary = sync_leased(
            season,
            _scheduled_client,
            schedule_changes=schedule_change_dispatcher(),
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


# Requests stop first; publication stops before the 230 s soft time limit and
# commits what it published.
HISTORY_REQUEST_SECONDS = 90
HISTORY_PUBLISH_SECONDS = 190
# History and the live sync share one worker process and alternate: history
# tasks outlive a live turn (beat expiry 300 s), and after a history turn the
# next ones step aside this long while the live sync has due work.
HISTORY_TURN_GAP_SECONDS = 240
HISTORY_TURN_KEY = "competition:history:last-turn"


@shared_task(ignore_result=True, soft_time_limit=230, time_limit=240)
def sync_competition_history() -> dict[str, object]:
    """Work through queued history imports while current-season work is idle."""
    if not settings.SPORTLINK_SYNC_ENABLED or not settings.SPORTLINK_SYNC_SESSION_FILE:
        return {"status": "disabled", "http_requests": 0}
    if settings.SPORTLINK_SCHEDULER != "legacy":
        return {
            "status": f"scheduler_{settings.SPORTLINK_SCHEDULER}",
            "http_requests": 0,
        }
    if not HistoricalResource.objects.filter(
        state="pending", next_attempt_at__lte=timezone.now()
    ).exists():
        return {"status": "idle", "http_requests": 0}
    last_turn = cache.get(HISTORY_TURN_KEY)
    if (
        last_turn is not None
        and time.time() - last_turn < HISTORY_TURN_GAP_SECONDS
        and live_work_waiting()
    ):
        return {"status": "yielded", "http_requests": 0}
    started = time.monotonic()
    try:
        summary = run_history(
            scheduled_history_client,
            publish_with=schedule_change_dispatcher(),
            budget=settings.SPORTLINK_HISTORY_MAX_REQUESTS,
            deadline=started + HISTORY_REQUEST_SECONDS,
            publish_deadline=started + HISTORY_PUBLISH_SECONDS,
        )
    except (OSError, ValueError, TypeError):
        logger.warning("Competition history cannot load its private OAuth session")
        return {"status": "session_unavailable", "http_requests": 0}
    logger.info("Competition history summary: %s", summary)
    if summary.get("reason") not in {"provider_lease_busy", "current_work_due"}:
        cache.set(HISTORY_TURN_KEY, time.time(), timeout=3600)
    return {"status": "ran", **summary}


@shared_task(ignore_result=True)
def recheck_competition_history() -> dict[str, object]:
    """Queue a small recheck of editions the provider did not serve.

    Only checkpoints change here; the provider turn sends the requests.
    """
    if not settings.SPORTLINK_SYNC_ENABLED:
        return {"status": "disabled"}
    editions = [
        recheck_edition(edition)
        for edition in settings.SPORTLINK_HISTORY_RECHECK_EDITIONS
        if edition < current_edition()
    ]
    logger.info("Competition history recheck: %s", editions)
    return {"status": "queued", "editions": editions}


def _provider_clients() -> tuple[CompetitionClient, HistoricalClient]:
    """Open the private session only after the provider lease is claimed.

    Raises:
        SessionUnavailableError: Credentials are missing, insecure or malformed.

    """
    try:
        return provider_clients()
    except (OSError, ValueError, TypeError):
        raise SessionUnavailableError from None


@shared_task(ignore_result=True, soft_time_limit=230, time_limit=240)
def run_provider_turn() -> dict[str, object]:
    """Run one provider turn: live and history requests under one lease."""
    if not settings.SPORTLINK_SYNC_ENABLED:
        return {"status": "disabled", "http_requests": 0}
    if settings.SPORTLINK_SCHEDULER != "unified":
        return {
            "status": f"scheduler_{settings.SPORTLINK_SCHEDULER}",
            "http_requests": 0,
        }
    if not settings.SPORTLINK_SYNC_SESSION_FILE:
        logger.warning("Provider turns require a private session file")
        return {"status": "configuration_required", "http_requests": 0}
    season = active_live_season()
    if season is None:
        return _provider_turn(None)
    return observe_run(season, lambda: _provider_turn(season))


def _provider_turn(season: Season | None) -> dict[str, object]:
    """Skip cheaply when blocked or idle; otherwise run and chain the next turn."""
    if match_forms_due():
        return {"status": "match_form_pending", "http_requests": 0}
    if SyncLease.objects.filter(
        key="sportlink", expires_at__gt=timezone.now()
    ).exists():
        return {"status": "busy_or_cooldown", "http_requests": 0}
    if not history_pending() and (
        season is None
        or not preview_sync(season, budget=None)["candidate_feed_requests"]
    ):
        return {"status": "idle", "http_requests": 0}
    options = TurnOptions(
        schedule_changes=schedule_change_dispatcher(),
        live_budget=settings.SPORTLINK_SYNC_MAX_REQUESTS or None,
        history_budget=settings.SPORTLINK_HISTORY_MAX_REQUESTS,
        history_share=settings.SPORTLINK_HISTORY_SHARE,
    )
    try:
        result = ProviderTurn(season, _provider_clients, options).run()
    except SessionUnavailableError:
        logger.warning("Provider turn cannot load its private OAuth session")
        return {"status": "session_unavailable", "http_requests": 0}
    logger.info("Provider turn summary: %s", result)
    if result.get("more_work"):
        # Keep the account busy instead of idling until the next beat tick.
        run_provider_turn.apply_async(expires=60)
    status = outcome(result) if season is not None else "completed"
    return {**result, "status": result.get("status", status)}


# Publication stops before the 230 s soft time limit; pending work re-queues.
PUBLICATION_SECONDS = 200


@shared_task(ignore_result=True, soft_time_limit=230, time_limit=240)
def publish_competition_backlog() -> dict[str, object]:
    """Publish imported data beside the provider manager (manager mode)."""
    if settings.SPORTLINK_SCHEDULER != "manager":
        return {"status": f"scheduler_{settings.SPORTLINK_SCHEDULER}"}
    owner = claim_publication()
    if owner is None:
        return {"status": "busy"}
    try:
        result = publish_backlog(
            schedule_changes=schedule_change_dispatcher(),
            live_season=active_live_season(),
            owner=owner,
            deadline=time.monotonic() + PUBLICATION_SECONDS,
        )
    finally:
        release_publication(owner)
    logger.info("Competition publication summary: %s", result)
    if result["more"]:
        publish_competition_backlog.apply_async(expires=120)
    return {"status": "published", **result}
