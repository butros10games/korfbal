"""Module contains `players_stats` function that returns player stats in a match."""

from collections.abc import Iterable
import json
import operator
from typing import Any, TypedDict

from asgiref.sync import sync_to_async
from django.db.models import Count, Q

from apps.game_tracker.models import Shot


class PlayerStatRow(TypedDict):
    """Structured representation of player statistics."""

    username: str
    shots_for: int
    shots_against: int
    goals_for: int
    goals_against: int


async def build_player_stats(
    players: list[Any], match_dataset: Iterable[Any]
) -> list[PlayerStatRow]:
    """Compute the raw player statistics for a collection of matches.

    Returns:
        list[PlayerStatRow]: List of player stats.

    """
    if not players:
        return []

    rows = await sync_to_async(list)(
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
        .order_by("-goals_for", "player__user__username"),
    )

    player_rows: list[PlayerStatRow] = [
        {
            "username": str(row.get("player__user__username") or ""),
            "shots_for": int(row.get("shots_for") or 0),
            "shots_against": int(row.get("shots_against") or 0),
            "goals_for": int(row.get("goals_for") or 0),
            "goals_against": int(row.get("goals_against") or 0),
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
