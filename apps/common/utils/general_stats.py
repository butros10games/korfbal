"""This module contains the general_stats function that returns the general statistics of a match."""  # noqa: E501

import json

from asgiref.sync import sync_to_async

from apps.game_tracker.models import GoalType, Shot


async def general_stats(match_dataset):
    """
    Return the general statistics of a match.

    Args:
        match_dataset {list} -- A list of multiple match data objects.

    Returns:
        str -- A JSON string containing the general statistics of the match.
    """
    goal_types = await sync_to_async(list)(GoalType.objects.all())

    goal_types_json = [
        {"id": str(goal_type.id_uuid), "name": goal_type.name}
        for goal_type in goal_types
    ]

    team_goal_stats = {}
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

    return json.dumps(
        {
            "command": "stats",
            "data": {
                "type": "general",
                "stats": {
                    "shots_for": await Shot.objects.filter(
                        match_data__in=match_dataset, for_team=True
                    ).acount(),
                    "shots_against": await Shot.objects.filter(
                        match_data__in=match_dataset, for_team=False
                    ).acount(),
                    "goals_for": await Shot.objects.filter(
                        match_data__in=match_dataset, for_team=True, scored=True
                    ).acount(),
                    "goals_against": await Shot.objects.filter(
                        match_data__in=match_dataset, for_team=False, scored=True
                    ).acount(),
                    "team_goal_stats": team_goal_stats,
                    "goal_types": goal_types_json,
                },
            },
        }
    )
