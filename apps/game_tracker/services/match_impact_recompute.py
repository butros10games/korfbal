"""Scheduling helpers for impact recomputation.

We want match editing (saving a Shot/Substitution/etc.) to remain responsive and
never fail because a background queue is unavailable.

So scheduling is best-effort:
- enqueue a Celery task on transaction commit
- swallow broker errors and log them
"""

from __future__ import annotations

from importlib import import_module
import logging
from typing import Any

from django.db import transaction


logger = logging.getLogger(__name__)


def schedule_match_impact_recompute(*, match_data_id: str) -> None:
    """Best-effort enqueue of the recompute task."""

    def _enqueue() -> None:
        try:
            # Avoid importing Celery tasks at module import time.
            tasks: Any = import_module("apps.game_tracker.tasks")
            task: Any = tasks.recompute_match_impacts
            task.delay(match_data_id)
        except Exception:
            logger.exception(
                "Failed to enqueue recompute_match_impacts(%s). "
                "Continuing without blocking.",
                match_data_id,
            )

    transaction.on_commit(_enqueue)
