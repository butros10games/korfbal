"""Module contains general_stats function that returns general statistics of a match."""

from collections.abc import Iterable
import json
from typing import Any

from asgiref.sync import sync_to_async
from django.db.models import Count, Q

from apps.game_tracker.models import GoalType, Shot


async def build_general_stats(match_dataset: Iterable[Any]) -> dict[str, Any]:
    """Assemble the general statistics payload for a collection of matches.

    Returns:
        dict[str, Any]: General stats payload.

    """
    goal_types = await sync_to_async(list)(
        GoalType.objects.all().values("id_uuid", "name").order_by("name"),
    )

    goal_types_json = [
        {"id": str(row["id_uuid"]), "name": row["name"]} for row in goal_types
    ]

    shot_qs = Shot.objects.filter(match_data__in=match_dataset)

    aggregated = await sync_to_async(
        lambda: shot_qs.aggregate(
            shots_for=Count("id_uuid", filter=Q(for_team=True)),
            shots_against=Count("id_uuid", filter=Q(for_team=False)),
            goals_for=Count("id_uuid", filter=Q(for_team=True, scored=True)),
            goals_against=Count("id_uuid", filter=Q(for_team=False, scored=True)),
        ),
    )()

    team_goal_stats: dict[str, dict[str, int]] = {
        row["name"]: {"goals_by_player": 0, "goals_against_player": 0}
        for row in goal_types
    }

    goal_type_rows = await sync_to_async(list)(
        shot_qs.filter(scored=True, shot_type__isnull=False)
        .values("shot_type__name", "for_team")
        .annotate(count=Count("id_uuid")),
    )

    for row in goal_type_rows:
        name = row.get("shot_type__name")
        if not name:
            continue
        entry = team_goal_stats.setdefault(
            str(name),
            {"goals_by_player": 0, "goals_against_player": 0},
        )
        if bool(row.get("for_team")):
            entry["goals_by_player"] = int(row.get("count") or 0)
        else:
            entry["goals_against_player"] = int(row.get("count") or 0)

    return {
        "shots_for": int(aggregated.get("shots_for") or 0),
        "shots_against": int(aggregated.get("shots_against") or 0),
        "goals_for": int(aggregated.get("goals_for") or 0),
        "goals_against": int(aggregated.get("goals_against") or 0),
        "team_goal_stats": team_goal_stats,
        "goal_types": goal_types_json,
    }


async def general_stats(match_dataset: Iterable[Any]) -> str:
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
