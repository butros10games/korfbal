"""Durable provider-wide limits count every HTTP attempt, including OAuth."""

from datetime import timedelta
import time
import uuid

from django.db import transaction
from django.utils import timezone

from apps.competition.application.ports import RequestBudgetError
from apps.competition.models import SyncLease, TrafficState


HOURLY_LIMIT = 120
DAILY_LIMIT = 1000
MAX_WAIT_SECONDS = 60


class TrafficGate:
    """Limit one provider to 120 requests/hour, 1000/day and five-second spacing."""

    def __init__(self, budget: int, owner: uuid.UUID) -> None:
        """Bind the per-run budget to the already claimed global provider lease."""
        self.budget = budget
        self.owner = owner
        self.requests = 0

    def before_request(self) -> None:
        """Reserve a wire request before sending it, never waiting out a quota.

        Raises:
            RequestBudgetError: A run, hourly or daily limit has been reached.
            ValueError: The importer no longer owns the provider lease.

        """
        now = timezone.now()
        if self.requests >= self.budget:
            raise RequestBudgetError(now)
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
            if state.hour_requests >= HOURLY_LIMIT:
                deadlines.append(state.hour_start + timedelta(hours=1))
            if state.day_requests >= DAILY_LIMIT:
                deadlines.append(state.day_start + timedelta(days=1))
            if deadlines:
                raise RequestBudgetError(max(deadlines))
            scheduled = max(now, state.next_request_at)
            if (scheduled - now).total_seconds() > MAX_WAIT_SECONDS:
                raise RequestBudgetError(scheduled)
            if not SyncLease.objects.filter(key="sportlink", owner=self.owner).update(
                expires_at=scheduled + timedelta(seconds=120)
            ):
                raise ValueError("Import lease was lost")
            state.hour_requests += 1
            state.day_requests += 1
            state.next_request_at = scheduled + timedelta(seconds=5)
            state.save()
        self.requests += 1
        time.sleep(max(0, (scheduled - timezone.now()).total_seconds()))
