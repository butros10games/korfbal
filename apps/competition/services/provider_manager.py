"""Long-running provider manager: one supervised loop sends all Sportlink requests.

Celery beat turns stopped at a time limit, published inside the turn and idled
until the next tick. The manager runs provider turns back to back in its own
supervised process: requests never wait for publication (a separate pool
publishes in parallel) or for a scheduler tick. Safety comes from the lease that
every request renews, per-request HTTP timeouts and database lock timeouts.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import logging

from django.conf import settings
from django.db import close_old_connections, connection
from django.utils import timezone

from apps.competition.application.ports import CompetitionClient, HistoricalClient
from apps.competition.models import SyncLease
from apps.competition.services.monitoring import observe_run, outcome
from apps.competition.services.provider_scheduler import (
    ProviderTurn,
    TurnOptions,
    active_live_season,
    history_pending,
)
from apps.competition.services.schedule_notifications import ScheduleChangeDispatcher
from apps.competition.services.sync import match_forms_due, preview_sync
from apps.schedule.models import Season


logger = logging.getLogger(__name__)

# Turns end to refresh the live planner snapshot, not because of a time limit.
TURN_SECONDS = 120
IDLE_SECONDS = 15
BLOCKED_SECONDS = 2
ERROR_SECONDS = 30


@dataclass(frozen=True)
class ManagerWiring:
    """Capabilities bound at the composition root."""

    clients: Callable[[], tuple[CompetitionClient, HistoricalClient]]
    schedule_changes: Callable[[], ScheduleChangeDispatcher]
    request_publication: Callable[[], None]


class ProviderManager:
    """Run provider turns back to back until asked to stop."""

    def __init__(
        self,
        wiring: ManagerWiring,
        *,
        stopping: Callable[[], bool],
        wait: Callable[[float], object],
    ) -> None:
        """Bind wiring, the stop signal and an interruptible wait."""
        self.wiring = wiring
        self.stopping = stopping
        self.wait = wait

    def run_forever(self) -> None:
        """Loop until stopped; one failing turn never stops the manager."""
        while not self.stopping():
            try:
                delay = self.step()
            except Exception:
                logger.exception("Provider manager turn failed")
                delay = ERROR_SECONDS
            if delay:
                self.wait(delay)

    def step(self) -> float:
        """Run one turn when there is work.

        Returns:
            Seconds to wait before the next step (0 to continue at once).

        """
        close_old_connections()
        limit_database_waits()
        if not (
            settings.SPORTLINK_SYNC_ENABLED and settings.SPORTLINK_SYNC_SESSION_FILE
        ):
            return IDLE_SECONDS
        if (
            match_forms_due()
            or SyncLease.objects.filter(
                key="sportlink", expires_at__gt=timezone.now()
            ).exists()
        ):
            return BLOCKED_SECONDS
        season = active_live_season()
        if not work_waiting(season):
            return IDLE_SECONDS
        result = (
            observe_run(season, lambda: self.turn(season))
            if season is not None
            else self.turn(None)
        )
        history = result.get("history") or {}
        if result.get("updated") or (
            isinstance(history, dict) and history.get("fetched")
        ):
            self.wiring.request_publication()
        if result.get("status") == "busy_or_cooldown":
            return BLOCKED_SECONDS
        return 0 if result.get("more_work") else IDLE_SECONDS

    def turn(self, season: Season | None) -> dict[str, object]:
        """Run one provider turn without publication."""
        options = TurnOptions(
            schedule_changes=self.wiring.schedule_changes(),
            request_seconds=TURN_SECONDS,
            live_budget=settings.SPORTLINK_SYNC_MAX_REQUESTS or None,
            history_budget=settings.SPORTLINK_HISTORY_MAX_REQUESTS,
            history_share=settings.SPORTLINK_HISTORY_SHARE,
            publish=False,
            stop=self.stopping,
        )
        result = ProviderTurn(season, self.wiring.clients, options).run()
        logger.info("Provider turn summary: %s", result)
        return {**result, "status": result.get("status") or outcome(result)}


def work_waiting(season: Season | None) -> bool:
    """Tell whether history or the live season has due work."""
    if history_pending():
        return True
    return season is not None and bool(
        preview_sync(season, budget=None)["candidate_feed_requests"]
    )


def limit_database_waits() -> None:
    """Bound lock waits so a blocked row cannot stall the manager indefinitely."""
    if connection.vendor == "postgresql":
        with connection.cursor() as cursor:
            cursor.execute("SET lock_timeout = '30s'")
            cursor.execute("SET statement_timeout = '120s'")
