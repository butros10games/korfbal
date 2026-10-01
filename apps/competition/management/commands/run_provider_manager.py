"""Run the long-running Sportlink provider manager (started by the worker)."""

from __future__ import annotations

import logging
import signal
import threading

from django.core.management.base import BaseCommand

from apps.competition.composition import (
    provider_clients,
    schedule_change_dispatcher,
)
from apps.competition.services.provider_manager import ManagerWiring, ProviderManager
from apps.competition.tasks import publish_competition_backlog


def request_publication() -> None:
    """Queue a publication pass; overlapping passes return at once."""
    publish_competition_backlog.apply_async(expires=120)


class Command(BaseCommand):
    """Send every provider request until SIGTERM, then release the lease."""

    help = "Long-running Sportlink provider manager (SPORTLINK_SCHEDULER=manager)."

    def handle(self, *args: object, **options: object) -> None:
        """Install signal handlers and loop until stopped."""
        # Outside Celery nothing configures logging, and Django's defaults drop
        # INFO: show turn summaries and HTTP reservations in the worker log.
        logging.basicConfig(
            level=logging.INFO,
            format="[%(asctime)s: %(levelname)s/provider-manager] %(message)s",
        )
        stopped = threading.Event()

        def stop(_signum: int, _frame: object) -> None:
            stopped.set()

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        ProviderManager(
            ManagerWiring(
                clients=provider_clients,
                schedule_changes=schedule_change_dispatcher,
                request_publication=request_publication,
            ),
            stopping=stopped.is_set,
            wait=stopped.wait,
        ).run_forever()
