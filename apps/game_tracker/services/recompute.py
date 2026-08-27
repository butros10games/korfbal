"""Best-effort scheduling shared by derived match projections."""

from __future__ import annotations

import logging
from typing import Protocol

from django.db import transaction


logger = logging.getLogger(__name__)


class RecomputeDispatcher(Protocol):
    """Dispatch one derived-data recomputation."""

    def __call__(
        self,
        *,
        match_data_id: str,
        countdown_seconds: int = 0,
    ) -> None:
        """Dispatch the recomputation."""


def schedule_recompute(
    *,
    match_data_id: str,
    countdown_seconds: int,
    dispatch: RecomputeDispatcher,
    task_name: str,
) -> None:
    """Enqueue derived-data work after commit without blocking mutations."""

    def enqueue() -> None:
        try:
            dispatch(
                match_data_id=match_data_id,
                countdown_seconds=countdown_seconds,
            )
        except Exception:
            logger.exception(
                "Failed to enqueue %s(%s); continuing without blocking.",
                task_name,
                match_data_id,
            )

    transaction.on_commit(enqueue)
