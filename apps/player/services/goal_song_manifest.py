"""Build the complete, stable audio manifest used by the match tracker."""

from __future__ import annotations

from collections.abc import Iterable
from urllib.parse import urlencode

from django.db.models import F, Subquery
from django.urls import reverse

from apps.player.models import (
    Player,
    PlayerGoalSongSelection,
    PlayerSong,
    PlayerSongStatus,
    TeamGoalSongSelection,
)
from apps.player.services.player_song_queries import player_songs_by_ids
from apps.schedule.models import Season
from apps.team.models import Team, TeamData


def _song_is_ready(song: PlayerSong) -> bool:
    return song.effective_status == PlayerSongStatus.READY and bool(
        song.effective_audio_file
    )


def _entry(song: PlayerSong) -> dict[str, object]:
    start_seconds = max(0, int(song.start_time_seconds or 0))
    audio_file = song.effective_audio_file
    source_updated_at = song.effective_updated_at
    query = urlencode({
        "start": start_seconds,
        "duration": song.clip_duration_seconds,
        "stream": 1,
        "v": (
            f"{getattr(audio_file, 'name', '')}:"
            f"{int(source_updated_at.timestamp() * 1_000_000)}"
        ),
    })
    return {
        "id": str(song.id_uuid),
        "url": (
            f"{reverse('player-song-clip', kwargs={'song_id': song.id_uuid})}?{query}"
        ),
        "playback_speed": float(song.playback_speed or 1.0),
    }


def build_goal_song_manifest(
    *,
    player_ids: Iterable[str],
    team: Team,
    season: Season | None,
) -> dict[str, object]:
    """Return player and team-fallback clips in deterministic selection order."""
    normalized_player_ids = list(dict.fromkeys(str(value) for value in player_ids))
    # Query canonical selection IDs without hydrating owners or selection models.
    # Keep the visible-player manager: a relation join alone bypasses its policy.
    player_selections: dict[str, list[str]] = {}
    if normalized_player_ids:
        selections = PlayerGoalSongSelection.objects.filter(
            player__in=Player.objects.filter(id_uuid__in=normalized_player_ids),
            song__player_id=F("player_id"),
        ).values_list("player_id", "song_id")
        for player_id, song_id in selections:
            player_selections.setdefault(str(player_id), []).append(str(song_id))

    if season is None:
        selected_team_data = (
            TeamData.objects
            .filter(team=team)
            .order_by("-season__start_date")
            .values("pk")[:1]
        )
        fallback_selections = TeamGoalSongSelection.objects.filter(
            team_data_id=Subquery(selected_team_data)
        )
    else:
        # TeamData is unique per team/season; only the latest-season lookup
        # needs to sort seasons and select an owner before inspecting songs.
        fallback_selections = TeamGoalSongSelection.objects.filter(
            team_data__team=team, team_data__season=season
        )
    fallback_rows = list(
        fallback_selections.values_list(
            "song_id", "song__player_id", "team_data_id", "song__team_data_id"
        )
    )
    fallback_ids: list[str] = []
    if fallback_rows:
        # Only selected fallback owners need a membership/privacy check.
        allowed_owners = {
            str(player_id)
            for player_id in Player.objects.filter(
                pk__in=[owner_id for _, owner_id, _, _ in fallback_rows],
                team_data_as_player=fallback_rows[0][2],
            ).values_list("pk", flat=True)
        }
        fallback_ids = [
            str(song_id)
            for song_id, owner_id, team_data_id, song_team_data_id in fallback_rows
            if str(owner_id) in allowed_owners or song_team_data_id == team_data_id
        ]

    selected_ids = {
        song_id for values in player_selections.values() for song_id in values
    }
    selected_ids.update(fallback_ids)
    songs = list(player_songs_by_ids(song_ids=selected_ids))
    songs_by_id = {str(song.id_uuid): song for song in songs if _song_is_ready(song)}

    players: dict[str, list[dict[str, object]]] = {}
    for player_id in normalized_player_ids:
        entries = [
            _entry(songs_by_id[song_id])
            for song_id in player_selections.get(player_id, [])
            if song_id in songs_by_id
            and str(songs_by_id[song_id].player_id) == player_id
        ]
        if entries:
            players[player_id] = entries

    fallback = [
        _entry(songs_by_id[song_id])
        for song_id in fallback_ids
        if song_id in songs_by_id
    ]

    return {"version": 1, "players": players, "fallback": fallback}
