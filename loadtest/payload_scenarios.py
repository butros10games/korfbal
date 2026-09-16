"""Synthetic command coverage for the isolated SSE payload audit."""

from collections.abc import Iterator
from typing import Any
from uuid import uuid4

from apps.game_tracker.models import GroupType, MatchData, MatchPart, MatchPlayer
from apps.game_tracker.services.player_groups import RESERVE_GROUP_NAME
from apps.game_tracker.tests.tracker_test_helpers import (
    create_player_group,
    create_tracker_player,
)
from apps.schedule.models import Match


def prepare_reserve(match: Match) -> str:
    """Add one synthetic reserve before capturing the initial public state."""
    data = MatchData.objects.get(match_link=match)
    reserve = create_tracker_player(username=f"audit-reserve-{uuid4().hex[:8]}")
    MatchPlayer.objects.create(match_data=data, team=match.home_team, player=reserve)
    group = create_player_group(
        match_data=data,
        team=match.home_team,
        group_type=GroupType.objects.get_or_create(name=RESERVE_GROUP_NAME)[0],
    )
    group.players.add(reserve)
    return str(reserve.pk)


def scenarios(
    match: Match, fixture: dict[str, Any], reserve: str
) -> Iterator[tuple[str, dict[str, Any]]]:
    """Exercise each registry command, including clock and period transitions.

    Yields:
        A descriptive scenario label and its command payload.

    """
    for index, command in enumerate(("shot_reg", "goal_reg", "shot_reg")):
        yield (
            command,
            {
                "command": command,
                "player_id": fixture["players"][index],
                "goal_type": fixture["goal_type"],
                "for_team": True,
            },
        )
    yield "pause", {"command": "start/pause"}
    yield "resume", {"command": "start/pause"}
    yield "timeout", {"command": "timeout", "for_team": True}
    yield "resume_after_timeout", {"command": "start/pause"}
    yield "ball_loss", {"command": "possession_change_reg", "kind": "ball_loss"}
    yield "interception", {"command": "possession_change_reg", "kind": "interception"}
    yield (
        "substitution",
        {
            "command": "substitute_reg",
            "old_player_id": fixture["players"][0],
            "new_player_id": reserve,
        },
    )
    yield "opponent_substitution", {"command": "substitute_against_reg"}
    yield "reserve_read", {"command": "get_non_active_players"}
    yield "new_attack", {"command": "new_attack"}
    yield "undo_event", {"command": "remove_last_event"}
    part = MatchPart.objects.get(match_data__match_link=match, active=True)
    yield "part_end", {"command": "part_end"}
    yield (
        "undo_part_end",
        {
            "command": "undo_part_transition",
            "part_id": str(part.pk),
            "transition": "end",
        },
    )
    yield "resume_after_undo", {"command": "start/pause"}
    yield "part_end_again", {"command": "part_end"}
    yield "next_part_start", {"command": "start/pause"}
    part = MatchPart.objects.get(match_data__match_link=match, active=True)
    yield (
        "undo_part_start",
        {
            "command": "undo_part_transition",
            "part_id": str(part.pk),
            "transition": "start",
        },
    )
