"""Build the complete, stable audio manifest used by the match tracker."""

from __future__ import annotations

from collections.abc import Iterable
from urllib.parse import urlencode

from django.urls import reverse

from apps.player.models import Player, PlayerSong, PlayerSongStatus
from apps.player.services.player_audio import GOAL_SONG_CLIP_DURATION_SECONDS
from apps.player.services.player_song_queries import player_songs_by_ids
from apps.schedule.models import Season
from apps.team.models import Team, TeamData


def _normalized_ids(values: object) -> list[str]:
    if not isinstance(values, list):
        return []
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if not isinstance(value, str):
            continue
        normalized = value.strip()
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return result


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
        "duration": GOAL_SONG_CLIP_DURATION_SECONDS,
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
    player_selections = {
        str(player_id): _normalized_ids(song_ids)
        for player_id, song_ids in Player.objects.filter(
            id_uuid__in=normalized_player_ids
        ).values_list("id_uuid", "goal_song_song_ids")
    }

    team_data_query = TeamData.objects.filter(team=team)
    if season is not None:
        team_data_query = team_data_query.filter(season=season)
    team_data = team_data_query.order_by("-season__start_date").first()
    fallback_ids = _normalized_ids(
        team_data.fallback_goal_song_song_ids if team_data is not None else []
    )

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

    allowed_fallback_player_ids: set[str] = set()
    if team_data is not None:
        allowed_fallback_player_ids = {
            str(value) for value in team_data.players.values_list("id_uuid", flat=True)
        }
    fallback = [
        _entry(songs_by_id[song_id])
        for song_id in fallback_ids
        if song_id in songs_by_id
        and str(songs_by_id[song_id].player_id) in allowed_fallback_player_ids
    ]

    return {"version": 1, "players": players, "fallback": fallback}
