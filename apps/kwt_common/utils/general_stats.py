"""Module contains general_stats function that returns general statistics of a match."""

import json
from typing import Any

from asgiref.sync import sync_to_async

from apps.game_tracker.models import GoalType, Shot


async def build_general_stats(match_dataset: list[Any]) -> dict[str, Any]:
    """Assemble the general statistics payload for a collection of matches.

    Returns:
        dict[str, Any]: General stats payload.

    """
    goal_types: list[GoalType] = await sync_to_async(list)(GoalType.objects.all())

    goal_types_json = [
        {"id": str(goal_type.id_uuid), "name": goal_type.name}
        for goal_type in goal_types
    ]

    team_goal_stats: dict[str, dict[str, int]] = {}
    for goal_type in goal_types:
        goals_for = await Shot.objects.filter(
            match_data__in=match_dataset,
            shot_type=goal_type,
            for_team=True,
            scored=True,
        ).acount()
        goals_against = await Shot.objects.filter(
            match_data__in=match_dataset,
            shot_type=goal_type,
            for_team=False,
            scored=True,
        ).acount()

        team_goal_stats[goal_type.name] = {
            "goals_by_player": goals_for,
            "goals_against_player": goals_against,
        }

    return {
        "shots_for": await Shot.objects.filter(
            match_data__in=match_dataset,
            for_team=True,
        ).acount(),
        "shots_against": await Shot.objects.filter(
            match_data__in=match_dataset,
            for_team=False,
        ).acount(),
        "goals_for": await Shot.objects.filter(
            match_data__in=match_dataset,
            for_team=True,
            scored=True,
        ).acount(),
        "goals_against": await Shot.objects.filter(
            match_data__in=match_dataset,
            for_team=False,
            scored=True,
        ).acount(),
        "team_goal_stats": team_goal_stats,
        "goal_types": goal_types_json,
    }


async def general_stats(match_dataset: list[Any]) -> str:
    """Return the general statistics of a match as JSON for websocket clients.

    Returns:
        str: JSON string of general stats.

    """
    stats = await build_general_stats(match_dataset)
    return json.dumps(
        {
            "command": "stats",
            "data": {
                "type": "general",
                "stats": stats,
            },
        },
    )
