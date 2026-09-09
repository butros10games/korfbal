"""Team impact category aggregation with best-effort breakdown repair."""

from __future__ import annotations

import logging
from typing import Any

from django.db.models import QuerySet

from apps.game_tracker.models import (
    MatchData,
    PlayerMatchImpact,
    PlayerMatchImpactBreakdown,
)
from apps.game_tracker.services.match_impact import (
    LATEST_MATCH_IMPACT_ALGORITHM_VERSION,
    persist_match_impact_rows_with_breakdowns,
)
from apps.player.models import Player
from apps.team.models.team import Team


logger = logging.getLogger(__name__)


def _impact_breakdown_for_impact(*, impact: PlayerMatchImpact) -> dict[str, Any]:
    breakdown_obj = getattr(impact, "breakdown", None)
    if (
        breakdown_obj is not None
        and breakdown_obj.algorithm_version == LATEST_MATCH_IMPACT_ALGORITHM_VERSION
        and isinstance(breakdown_obj.breakdown, dict)
    ):
        return breakdown_obj.breakdown

    # Best-effort: compute+persist breakdowns so next request is fast.
    try:
        persist_match_impact_rows_with_breakdowns(
            match_data=impact.match_data,
            algorithm_version=LATEST_MATCH_IMPACT_ALGORITHM_VERSION,
        )
    except Exception:
        logger.exception("Failed to rebuild impact breakdown for %s", impact.pk)
        return {}

    refreshed = (
        PlayerMatchImpactBreakdown.objects
        .filter(
            impact=impact,
            algorithm_version=LATEST_MATCH_IMPACT_ALGORITHM_VERSION,
        )
        .only("breakdown")
        .first()
    )
    if refreshed is None or not isinstance(refreshed.breakdown, dict):
        return {}
    return refreshed.breakdown


def aggregate_player_impact_breakdowns(
    *,
    team: Team,
    player: Player,
    match_data_qs: QuerySet[MatchData],
) -> tuple[int, float, dict[str, dict[str, float | int]]]:
    """Aggregate impact categories, repairing missing breakdowns when possible."""
    aggregated: dict[str, dict[str, float | int]] = {}
    matches_considered = 0
    impact_total_raw = 0.0

    impacts_qs = (
        PlayerMatchImpact.objects
        .filter(
            match_data__in=match_data_qs,
            player=player,
            team=team,
            algorithm_version=LATEST_MATCH_IMPACT_ALGORITHM_VERSION,
        )
        .select_related("match_data")
        .select_related("breakdown")
    )

    for impact in impacts_qs.iterator():
        matches_considered += 1
        impact_total_raw += float(impact.impact_score)

        per_player = _impact_breakdown_for_impact(impact=impact)
        for key, item in per_player.items():
            if key not in aggregated:
                aggregated[key] = {"points": 0.0, "count": 0}
            aggregated[key]["points"] = float(aggregated[key]["points"]) + float(
                item["points"]
            )
            aggregated[key]["count"] = int(aggregated[key]["count"]) + int(
                item["count"]
            )

    return matches_considered, impact_total_raw, aggregated
