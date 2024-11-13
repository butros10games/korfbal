from asgiref.sync import sync_to_async

from apps.game_tracker.models import Shot, GoalType

import json


async def general_stats(match_datas):
    goal_types = await sync_to_async(list)(GoalType.objects.all())

    goal_types_json = [
        {"id": str(goal_type.id_uuid), "name": goal_type.name}
        for goal_type in goal_types
    ]

    team_goal_stats = {}
    for goal_type in goal_types:
        goals_for = await Shot.objects.filter(
            match_data__in=match_datas, shot_type=goal_type, for_team=True, scored=True
        ).acount()
        goals_against = await Shot.objects.filter(
            match_data__in=match_datas, shot_type=goal_type, for_team=False, scored=True
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
                        match_data__in=match_datas, for_team=True
                    ).acount(),
                    "shots_against": await Shot.objects.filter(
                        match_data__in=match_datas, for_team=False
                    ).acount(),
                    "goals_for": await Shot.objects.filter(
                        match_data__in=match_datas, for_team=True, scored=True
                    ).acount(),
                    "goals_against": await Shot.objects.filter(
                        match_data__in=match_datas, for_team=False, scored=True
                    ).acount(),
                    "team_goal_stats": team_goal_stats,
                    "goal_types": goal_types_json,
                }
            }
        }
    )
