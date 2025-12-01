"""Module contains `players_stats` function that returns player stats in a match."""

import json
import operator
from typing import Any, TypedDict

from apps.game_tracker.models import Shot


class PlayerStatRow(TypedDict):
    """Structured representation of player statistics."""

    username: str
    shots_for: int
    shots_against: int
    goals_for: int
    goals_against: int


async def build_player_stats(
    players: list[Any], match_dataset: list[Any]
) -> list[PlayerStatRow]:
    """Compute the raw player statistics for a collection of matches.

    Returns:
        list[PlayerStatRow]: List of player stats.

    """
    player_rows: list[PlayerStatRow] = []
    for player in players:
        row: PlayerStatRow = {
            "username": player.user.username,
            "shots_for": await Shot.objects.filter(
                match_data__in=match_dataset,
                player=player,
                for_team=True,
            ).acount(),
            "shots_against": await Shot.objects.filter(
                match_data__in=match_dataset,
                player=player,
                for_team=False,
            ).acount(),
            "goals_for": await Shot.objects.filter(
                match_data__in=match_dataset,
                player=player,
                for_team=True,
                scored=True,
            ).acount(),
            "goals_against": await Shot.objects.filter(
                match_data__in=match_dataset,
                player=player,
                for_team=False,
                scored=True,
            ).acount(),
        }
        player_rows.append(row)

    player_rows_sorted = sorted(
        player_rows,
        key=operator.itemgetter("goals_for"),
        reverse=True,
    )
    return [
        row
        for row in player_rows_sorted
        if row["shots_for"] > 0 or row["shots_against"] > 0
    ]


async def players_stats(players: list[Any], match_dataset: list[Any]) -> str:
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
