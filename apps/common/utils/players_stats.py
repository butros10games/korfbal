"""Module contains `players_stats` function that returns player stats in a match."""

import json
import operator

from apps.game_tracker.models import Shot


async def players_stats(players: list, match_dataset: list) -> str:
    """Return statistics of players in a match.

    Args:
        players (list): A list of player objects whose statistics are to be calculated.
        match_dataset (list): A list of match data elements to filter statistics.

    Returns:
        str: A JSON string containing statistics of players in the match.

    """
    players_stats = []
    for player in players:
        player_stats = {
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

        players_stats.append(player_stats)
    # sort the `player_stats` so the player with the most goals for is on top
    players_stats = sorted(
        players_stats, key=operator.itemgetter("goals_for"), reverse=True
    )

    # remove all the players with no shots for or against
    players_stats = [
        player
        for player in players_stats
        if (player["shots_for"] > 0 or player["shots_against"] > 0)
    ]

    return json.dumps(
        {
            "command": "stats",
            "data": {"type": "player_stats", "stats": {"player_stats": players_stats}},
        },
    )
