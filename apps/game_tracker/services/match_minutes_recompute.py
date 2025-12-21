"""Scheduling helpers for minutes-played recomputation.

Mirrors match_impact_recompute:
- enqueue a Celery task on transaction commit
- swallow broker errors and log them

"""

from __future__ import annotations

from importlib import import_module
import logging
from typing import Any

from django.db import transaction


logger = logging.getLogger(__name__)


def schedule_match_minutes_recompute(
    *,
    match_data_id: str,
    countdown_seconds: int = 0,
) -> None:
    """Best-effort enqueue of the recompute minutes task."""

    def _enqueue() -> None:
        try:
            tasks: Any = import_module("apps.game_tracker.tasks")
            task: Any = tasks.recompute_match_minutes
            if countdown_seconds > 0:
                task.apply_async(args=(match_data_id,), countdown=countdown_seconds)
            else:
                task.delay(match_data_id)
        except Exception:
            logger.exception(
                "Failed to enqueue recompute_match_minutes(%s). "
                "Continuing without blocking.",
                match_data_id,
            )

    transaction.on_commit(_enqueue)
