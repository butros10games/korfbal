"""Celery tasks for game_tracker."""

from __future__ import annotations

import logging

from celery import shared_task

from apps.game_tracker.models import MatchData
from apps.game_tracker.services.match_impact import (
    persist_match_impact_rows_with_breakdowns,
)
from apps.game_tracker.services.match_minutes import persist_match_minutes


logger = logging.getLogger(__name__)


@shared_task(bind=True, autoretry_for=(Exception,), retry_backoff=True, max_retries=3)
def recompute_match_impacts(self, match_data_id: str) -> dict[str, int | str]:  # noqa: ANN001
    """Recompute persisted impact rows (+ breakdowns) for a match."""
    match_data = (
        MatchData.objects
        .filter(id_uuid=match_data_id)
        .select_related("match_link")
        .first()
    )
    if not match_data:
        return {"match_data_id": match_data_id, "rows": 0, "status": "not_found"}

    rows = persist_match_impact_rows_with_breakdowns(match_data=match_data)
    logger.info("Recomputed match impacts for %s (%s rows)", match_data_id, rows)
    return {"match_data_id": match_data_id, "rows": rows, "status": "ok"}


@shared_task(bind=True, autoretry_for=(Exception,), retry_backoff=True, max_retries=3)
def recompute_match_minutes(self, match_data_id: str) -> dict[str, int | str]:  # noqa: ANN001
    """Recompute persisted minutes-played rows for a match."""
    match_data = (
        MatchData.objects
        .filter(id_uuid=match_data_id)
        .select_related("match_link")
        .first()
    )
    if not match_data:
        return {"match_data_id": match_data_id, "rows": 0, "status": "not_found"}

    rows = persist_match_minutes(match_data=match_data)
    logger.info("Recomputed match minutes for %s (%s rows)", match_data_id, rows)
    return {"match_data_id": match_data_id, "rows": rows, "status": "ok"}
