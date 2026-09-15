"""Seed real match commands and normal session authentication in a fresh database."""

from datetime import timedelta
from typing import Any

from django.conf import settings
from django.db import connection
from django.test import Client
from django.utils import timezone

from apps.game_tracker.composition import apply_tracker_command
from apps.game_tracker.models import GoalType, GroupType, MatchPlayer
from apps.game_tracker.tests.tracker_test_helpers import (
    create_player_group,
    create_tracker_match,
    create_tracker_player,
    login_home_club_editor,
)


def seed(matches: int, shots: int) -> list[dict[str, Any]]:
    """Build populated matches through real tracker commands.

    Raises:
        RuntimeError: The destination is not the disposable database.

    """
    if settings.DATABASES["default"]["NAME"] != "korfbal_loadtest":
        raise RuntimeError("Seeding requires the disposable load-test database.")
    with connection.cursor() as cursor:
        cursor.execute("CREATE EXTENSION IF NOT EXISTS pg_stat_statements")
    goal_type, _ = GoalType.objects.get_or_create(name="Load test shot")
    fixtures = []
    for index in range(matches):
        tracker = create_tracker_match(prefix=f"Synthetic {index}")
        client = Client()
        actor = login_home_club_editor(client, tracker, f"synthetic-coach-{index}")
        players = []
        group_types = [
            GroupType.objects.get_or_create(name=name)[0]
            for name in ("Aanval", "Verdediging")
        ]
        for team_index, team in enumerate((tracker.home_team, tracker.away_team)):
            groups = [
                create_player_group(
                    match_data=tracker.match_data, team=team, group_type=group_type
                )
                for group_type in group_types
            ]
            for player_index in range(8):
                player = create_tracker_player(
                    username=f"synthetic-{index}-{team_index}-{player_index}"
                )
                MatchPlayer.objects.create(
                    match_data=tracker.match_data, team=team, player=player
                )
                groups[player_index // 4].players.add(player)
                if team_index == 0:
                    players.append(str(player.pk))
        apply_tracker_command(
            tracker.match,
            team=tracker.home_team,
            payload={"command": "start/pause"},
            actor=actor,
        )
        for shot in range(shots):
            apply_tracker_command(
                tracker.match,
                team=tracker.home_team,
                actor=actor,
                payload={
                    "command": "goal_reg" if shot % 5 == 0 else "shot_reg",
                    "player_id": players[shot % len(players)],
                    "goal_type": str(goal_type.pk),
                    "for_team": shot % 3 != 0,
                    "client_time_ms": int(
                        (timezone.now() - timedelta(seconds=shots - shot)).timestamp()
                        * 1000
                    ),
                },
            )
        fixtures.append({
            "match_id": str(tracker.match.pk),
            "team_id": str(tracker.home_team.pk),
            "players": players,
            "goal_type": str(goal_type.pk),
            "session": client.cookies["sessionid"].value,
        })
    return fixtures
