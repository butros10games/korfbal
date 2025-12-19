"""Module contains `players_stats` function that returns player stats in a match.

Team/season pages should report impact scores consistent with the Match page.

We achieve this by:
- persisting per-player per-match impact rows using the match-page algorithm
- aggregating those persisted rows for team/season totals

When persisted rows are missing or outdated, we opportunistically recompute them.
"""

from __future__ import annotations

from collections.abc import Iterable
import json
import logging
import operator
from typing import Any, TypedDict, cast

from asgiref.sync import sync_to_async
from django.db.models import Count, Q, QuerySet, Sum

from apps.game_tracker.models import MatchData, PlayerMatchImpact, Shot
from apps.game_tracker.services.match_impact import (
    LATEST_MATCH_IMPACT_ALGORITHM_VERSION,
    persist_match_impact_rows,
)


logger = logging.getLogger(__name__)


class PlayerStatRow(TypedDict):
    """Structured representation of player statistics."""

    username: str
    shots_for: int
    shots_against: int
    goals_for: int
    goals_against: int
    impact_score: float
    impact_is_stored: bool


def _safe_ratio(numerator: float, denominator: float) -> float:
    if denominator <= 0:
        return 0.0
    return numerator / denominator


def _compute_impact_score(*, gf: int, ga: int, sf: int, sa: int) -> float:
    """Compute a lightweight, aggregated impact score.

    This intentionally mirrors the frontend heuristic used in
    `PlayerStatsTable.computeImpactScore` so that team-season stats can be
    computed server-side without fetching match timelines.
    """
    acc_for = _safe_ratio(gf, sf)
    acc_against = _safe_ratio(ga, sa)
    efficiency_delta = acc_for - acc_against

    raw = gf * 8 - ga * 6 + (sf - sa) * 1.25 + efficiency_delta * 10
    return round(float(raw), 1)


def _ensure_latest_match_impacts(*, match_dataset: Iterable[Any]) -> None:
    """Ensure that finished matches have up-to-date persisted impact rows.

    The Team page aggregates `PlayerMatchImpact` across a season. If those rows
    are missing (or were computed with an older algorithm version), the totals
    diverge from the Match page.

    This function opportunistically recomputes missing/outdated impacts.
    It must be safe to call during request handling.
    """
    # MatchData is always expected in practice (TeamViewSet passes a QuerySet),
    # but keep this defensive to avoid breaking other call sites.
    try:
        if isinstance(match_dataset, QuerySet) and match_dataset.model is MatchData:
            match_qs = cast(QuerySet[MatchData], match_dataset)
        else:
            match_ids: list[str] = []
            for item in match_dataset:
                mid = getattr(item, "id_uuid", None)
                if mid is None:
                    continue
                match_ids.append(str(mid))

            if not match_ids:
                return
            match_qs = MatchData.objects.filter(id_uuid__in=match_ids)

        needs_recompute = (
            match_qs.filter(status="finished")
            # Recompute if there are no impacts at the latest version.
            .exclude(
                player_impacts__algorithm_version=LATEST_MATCH_IMPACT_ALGORITHM_VERSION
            )
            .distinct()
            .select_related("match_link")
        )

        for match_data in needs_recompute:
            try:
                rows = persist_match_impact_rows(
                    match_data=match_data,
                    algorithm_version=LATEST_MATCH_IMPACT_ALGORITHM_VERSION,
                )
                logger.info(
                    "Computed match impacts for %s (%s rows)",
                    match_data.id_uuid,
                    rows,
                )
            except Exception:
                # Never fail the team page because a single match can't be
                # recomputed. We'll fall back to the heuristic impact for any
                # players that are still missing persisted values.
                logger.exception(
                    "Failed to compute match impacts for %s", match_data.id_uuid
                )
    except Exception:
        logger.exception("Failed to ensure latest match impacts")


async def build_player_stats(
    players: list[Any], match_dataset: Iterable[Any]
) -> list[PlayerStatRow]:
    """Compute the raw player statistics for a collection of matches.

    Returns:
        list[PlayerStatRow]: List of player stats.

    """
    if not players:
        return []

    def _fetch() -> tuple[list[dict[str, object]], dict[str, float]]:
        _ensure_latest_match_impacts(match_dataset=match_dataset)

        shot_rows = list(
            Shot.objects.filter(
                match_data__in=match_dataset,
                player__in=players,
            )
            .values("player__user__username")
            .annotate(
                shots_for=Count("id_uuid", filter=Q(for_team=True)),
                shots_against=Count("id_uuid", filter=Q(for_team=False)),
                goals_for=Count("id_uuid", filter=Q(for_team=True, scored=True)),
                goals_against=Count("id_uuid", filter=Q(for_team=False, scored=True)),
            )
            .order_by("-goals_for", "player__user__username")
        )

        impact_rows = (
            PlayerMatchImpact.objects.filter(
                match_data__in=match_dataset,
                player__in=players,
                algorithm_version=LATEST_MATCH_IMPACT_ALGORITHM_VERSION,
            )
            .values("player__user__username")
            .annotate(total=Sum("impact_score"))
        )

        impact_by_username: dict[str, float] = {}
        for row in impact_rows:
            username = str(row.get("player__user__username") or "").strip()
            total = row.get("total")
            if not username or total is None:
                continue
            impact_by_username[username] = round(float(total), 1)

        return shot_rows, impact_by_username

    rows, impact_by_username = await sync_to_async(_fetch)()

    player_rows: list[PlayerStatRow] = [  # type: ignore[invalid-assignment]
        {
            "username": (username := str(row.get("player__user__username") or "")),
            "shots_for": (sf := int(cast(int, row.get("shots_for") or 0))),
            "shots_against": (sa := int(cast(int, row.get("shots_against") or 0))),
            "goals_for": (gf := int(cast(int, row.get("goals_for") or 0))),
            "goals_against": (ga := int(cast(int, row.get("goals_against") or 0))),
            "impact_score": impact_by_username.get(
                username,
                _compute_impact_score(gf=gf, ga=ga, sf=sf, sa=sa),
            ),
            "impact_is_stored": username in impact_by_username,
        }
        for row in rows
        if row.get("player__user__username")
    ]

    return sorted(player_rows, key=operator.itemgetter("goals_for"), reverse=True)


async def players_stats(players: list[Any], match_dataset: Iterable[Any]) -> str:
    """Return statistics of players in a match as websocket-friendly JSON.

    Returns:
        str: JSON string of player stats.

    """
    player_rows = await build_player_stats(players, match_dataset)
    return json.dumps(
        {
            "command": "stats",
            "data": {"type": "player_stats", "stats": {"player_stats": player_rows}},
        },
    )
