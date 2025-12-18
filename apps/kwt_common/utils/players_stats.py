"""Module contains `players_stats` function that returns player stats in a match."""

from collections.abc import Iterable
import json
import operator
from typing import Any, TypedDict, cast

from asgiref.sync import sync_to_async
from django.db.models import Count, Q, Sum

from apps.game_tracker.models import PlayerMatchImpact, Shot


class PlayerStatRow(TypedDict):
    """Structured representation of player statistics."""

    username: str
    shots_for: int
    shots_against: int
    goals_for: int
    goals_against: int
    impact_score: float


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
            "username": str(row.get("player__user__username") or ""),
            "shots_for": (sf := int(cast(int, row.get("shots_for") or 0))),
            "shots_against": (sa := int(cast(int, row.get("shots_against") or 0))),
            "goals_for": (gf := int(cast(int, row.get("goals_for") or 0))),
            "goals_against": (ga := int(cast(int, row.get("goals_against") or 0))),
            "impact_score": impact_by_username.get(
                str(row.get("player__user__username") or ""),
                _compute_impact_score(gf=gf, ga=ga, sf=sf, sa=sa),
            ),
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
