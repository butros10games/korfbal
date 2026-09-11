"""Stable Celery entry points for revision-guarded match statistics."""

from collections.abc import Callable

from celery import shared_task

from apps.game_tracker.models import MatchData
from apps.game_tracker.services.match_impact import (
    persist_match_impact_rows_with_breakdowns,
)
from apps.game_tracker.services.match_minutes import persist_match_minutes


def _recompute(match_data_id: str, persist: Callable[..., int]) -> dict[str, int | str]:
    match_data = (
        MatchData.objects.select_related("match_link").filter(pk=match_data_id).first()
    )
    rows = persist(match_data=match_data) if match_data else 0
    return {
        "match_data_id": match_data_id,
        "rows": rows,
        "status": "ok" if match_data else "not_found",
    }


@shared_task(ignore_result=True)
def recompute_match_impacts(match_data_id: str) -> dict[str, int | str]:
    """Compatibility entry point for previously published impact jobs."""
    return _recompute(match_data_id, persist_match_impact_rows_with_breakdowns)


@shared_task(ignore_result=True)
def recompute_match_minutes(match_data_id: str) -> dict[str, int | str]:
    """Compatibility entry point for previously published minutes jobs."""
    return _recompute(match_data_id, persist_match_minutes)


@shared_task(ignore_result=True)
def recompute_match_statistics(match_data_id: str) -> None:
    """One durable generation rebuilds both projections; retries belong to its job."""
    recompute_match_impacts.run(match_data_id)
    recompute_match_minutes.run(match_data_id)
