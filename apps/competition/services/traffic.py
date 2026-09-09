"""Durable provider-wide limits count every HTTP attempt, including OAuth."""

from datetime import timedelta
import logging
import time
import uuid

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.competition.application.ports import RequestBudgetError
from apps.competition.models import SyncLease, TrafficState


logger = logging.getLogger(__name__)
MAX_WAIT_SECONDS = 60


class LeaseLostError(RuntimeError):
    """Stop a stale worker before it can issue unowned provider traffic."""


class TrafficGate:
    """Serialize paced traffic with optional operator quotas and a run deadline."""

    def __init__(
        self, budget: int | None, owner: uuid.UUID, *, deadline: float | None = None
    ) -> None:
        """Bind the per-run budget to the already claimed global provider lease."""
        self.budget = budget
        self.owner = owner
        self.requests = 0
        self.deadline = deadline

    def _check_deadline(self, wait_seconds: float = 0) -> None:
        """Do not start another HTTP request beyond the worker's time window.

        Raises:
            RequestBudgetError: Pacing or elapsed work has consumed the run window.

        """
        if (
            self.deadline is not None
            and time.monotonic() + wait_seconds >= self.deadline
        ):
            raise RequestBudgetError(timezone.now())

    def before_request(self) -> None:
        """Reserve a wire request before sending it, never waiting out a quota.

        Raises:
            RequestBudgetError: A run, hourly or daily limit has been reached.
            LeaseLostError: The importer no longer owns the provider lease.

        """
        now = timezone.now()
        if self.budget is not None and self.requests >= self.budget:
            raise RequestBudgetError(now)
        self._check_deadline()
        with transaction.atomic():
            state, _ = TrafficState.objects.select_for_update().get_or_create(
                key="sportlink",
                defaults={"hour_start": now, "day_start": now, "next_request_at": now},
            )
            if now >= state.hour_start + timedelta(hours=1):
                state.hour_start, state.hour_requests = now, 0
            if now >= state.day_start + timedelta(days=1):
                state.day_start, state.day_requests = now, 0
            deadlines = []
            hourly = settings.SPORTLINK_HOURLY_LIMIT
            daily = settings.SPORTLINK_DAILY_LIMIT
            spacing = max(1, settings.SPORTLINK_REQUEST_SPACING)
            if hourly and state.hour_requests >= hourly:
                deadlines.append(state.hour_start + timedelta(hours=1))
            if daily and state.day_requests >= daily:
                deadlines.append(state.day_start + timedelta(days=1))
            if deadlines:
                raise RequestBudgetError(max(deadlines))
            scheduled = max(now, state.next_request_at)
            wait_seconds = max(0, (scheduled - now).total_seconds())
            self._check_deadline(wait_seconds)
            if wait_seconds > MAX_WAIT_SECONDS:
                raise RequestBudgetError(scheduled)
            if not SyncLease.objects.filter(key="sportlink", owner=self.owner).update(
                expires_at=scheduled + timedelta(seconds=120)
            ):
                raise LeaseLostError("Import lease was lost")
            state.hour_requests += 1
            state.day_requests += 1
            state.next_request_at = scheduled + timedelta(seconds=spacing)
            state.save()
        self.requests += 1
        logger.info(
            "Competition HTTP reservation at=%s hour=%s day=%s",
            scheduled.isoformat(),
            state.hour_requests,
            state.day_requests,
        )
        time.sleep(max(0, (scheduled - timezone.now()).total_seconds()))
        self._check_deadline()
        # A suspended process may wake after another worker has taken its lease.
        # Reservations remain conservative, but the stale worker must not send.
        now = timezone.now()
        if not SyncLease.objects.filter(
            key="sportlink", owner=self.owner, expires_at__gt=now
        ).exists():
            raise LeaseLostError("Import lease was lost")


def observe_rate_limit() -> None:
    """Record a provider rate-limit observation without inventing permanent quotas."""
    TrafficState.objects.filter(key="sportlink").update(rate_limited=True)
