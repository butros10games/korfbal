"""Match impact response data and best-effort persisted-row repair."""

from __future__ import annotations

from dataclasses import asdict
import logging
from typing import Any

from django.core.cache import cache

from apps.game_tracker.domain.win_probability import WPA_MODEL_VERSION
from apps.game_tracker.models import MatchData, PlayerMatchImpact
from apps.game_tracker.services.match_impact import (
    LATEST_MATCH_IMPACT_ALGORITHM_VERSION,
    compute_match_impact_contributions,
    persist_match_impact_rows,
)
from apps.schedule.models import Match


logger = logging.getLogger(__name__)


def _self_heal_latest_impacts_for_finished_match(*, match_data: MatchData) -> None:
    """Best-effort: persist latest impacts for finished matches when missing."""
    if match_data.status != "finished":
        return

    has_latest = PlayerMatchImpact.objects.filter(
        match_data=match_data,
        algorithm_version=LATEST_MATCH_IMPACT_ALGORITHM_VERSION,
    ).exists()
    if has_latest:
        return

    lock_key = (
        f"korfbal:match-impacts-selfheal:{match_data.id_uuid}:"
        f"{LATEST_MATCH_IMPACT_ALGORITHM_VERSION}"
    )
    if not cache.add(lock_key, "1", timeout=60 * 10):
        return

    try:
        persist_match_impact_rows(
            match_data=match_data,
            algorithm_version=LATEST_MATCH_IMPACT_ALGORITHM_VERSION,
        )
    except Exception:
        # Never fail the endpoint; the frontend may still fall back (or retry)
        # if impacts are unavailable.
        logger.exception(
            "Failed to self-heal match impacts for %s",
            match_data.id_uuid,
        )


def build_match_impacts_payload(
    *, match: Match, match_data: MatchData | None
) -> dict[str, Any]:
    """Build impact scores and contributions, repairing missing finished-match rows."""
    if not match_data:
        return {
            "match_data_id": None,
            "status": "unknown",
            "algorithm_version": LATEST_MATCH_IMPACT_ALGORITHM_VERSION,
            "score_unit": "expected_goal_value_added",
            "wpa_unit": "win_expectancy_added",
            "win_probability_model": WPA_MODEL_VERSION,
            "computed_at": None,
            "impacts": [],
        }
    _self_heal_latest_impacts_for_finished_match(match_data=match_data)
    impacts = list(
        PlayerMatchImpact.objects
        .filter(
            match_data=match_data,
            algorithm_version=LATEST_MATCH_IMPACT_ALGORITHM_VERSION,
        )
        .select_related("player", "team")
        .order_by("-impact_score", "player__user__username")
    )
    computed_at = None
    if impacts:
        computed_at = max(impact.computed_at for impact in impacts).isoformat()
    contributions_by_player: dict[str, list[dict[str, object]]] = {}
    for contribution in compute_match_impact_contributions(match_data=match_data):
        contributions_by_player.setdefault(contribution.player_id, []).append(
            asdict(contribution)
        )

    def _side_for_team_id(team_id: str | None) -> str | None:
        if not team_id:
            return None
        if team_id == str(match.home_team_id):
            return "home"
        if team_id == str(match.away_team_id):
            return "away"
        return None

    return {
        "match_data_id": str(match_data.id_uuid),
        "status": match_data.status,
        "algorithm_version": LATEST_MATCH_IMPACT_ALGORITHM_VERSION,
        "score_unit": "expected_goal_value_added",
        "wpa_unit": "win_expectancy_added",
        "win_probability_model": WPA_MODEL_VERSION,
        "computed_at": computed_at,
        "impacts": [
            {
                "player_id_uuid": str(impact.player_id),
                "team_id_uuid": str(impact.team_id) if impact.team_id else None,
                "team_side": _side_for_team_id(
                    str(impact.team_id) if impact.team_id else None
                ),
                "impact_score": float(impact.impact_score),
                "win_probability_added": float(impact.win_probability_added),
                "contributions": contributions_by_player.get(str(impact.player_id), []),
            }
            for impact in impacts
        ],
    }
