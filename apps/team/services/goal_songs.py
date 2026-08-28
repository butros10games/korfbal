"""Team-level commands that coordinate player and fallback goal songs."""

from __future__ import annotations

from django.db import transaction

from apps.player.models.player import Player
from apps.player.services.player_songs import delete_owned_player_song
from apps.team.models.team_data import TeamData


@transaction.atomic
def delete_team_player_song(
    *,
    player: Player,
    song_id: str,
    team_data: TeamData | None,
) -> None:
    """Delete an owned song and remove it from the team's fallback selection."""
    if team_data is not None:
        team_data = TeamData.objects.select_for_update().get(pk=team_data.pk)
    delete_owned_player_song(player=player, song_id=song_id)
    if team_data is None:
        return

    fallback_ids = [
        value for value in (team_data.fallback_goal_song_song_ids or []) if value
    ]
    next_ids = [value for value in fallback_ids if value != song_id]
    if next_ids == fallback_ids:
        return

    team_data.fallback_goal_song_song_ids = next_ids
    team_data.save(update_fields=["fallback_goal_song_song_ids"])
