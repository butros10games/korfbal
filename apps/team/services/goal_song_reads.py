"""Ordered goal-song playlists and playable fallback audio for team reads."""

from __future__ import annotations

from apps.player.models.player_song import PlayerSong, PlayerSongStatus
from apps.player.services.player_song_queries import player_songs_by_ids
from apps.schedule.models import Season
from apps.team.models.team import Team
from apps.team.queries.overview import main_roster_ids, team_data_for_season


def fallback_goal_song_song_ids(
    *,
    team: Team,
    season: Season | None,
) -> list[str]:
    """Normalize the selected team roster's fallback playlist in stored order."""
    team_data = team_data_for_season(team=team, season=season)
    if team_data is None:
        return []
    seen: set[str] = set()
    normalized: list[str] = []
    for entry in team_data.fallback_goal_song_song_ids or []:
        if not isinstance(entry, str):
            continue
        song_id = entry.strip()
        if not song_id or song_id in seen:
            continue
        seen.add(song_id)
        normalized.append(song_id)
    return normalized


def _song_entry(song: PlayerSong) -> dict[str, object] | None:
    audio_file = song.effective_audio_file
    if song.effective_status != PlayerSongStatus.READY or not audio_file:
        return None
    return {
        "id_uuid": str(song.id_uuid),
        "audio_url": audio_file.url,
        "start_time_seconds": int(song.start_time_seconds or 0),
        "playback_speed": float(song.playback_speed or 1.0),
        "title": song.effective_title,
        "artists": song.effective_artists,
        "player_id": str(song.player_id),
    }


def song_entries_for_ids(
    *,
    songs: list[PlayerSong],
    ids: list[str],
) -> list[dict[str, object]]:
    """Return playable song entries in selection order, skipping unavailable songs."""
    by_id = {str(song.id_uuid): song for song in songs}
    ordered: list[dict[str, object]] = []
    for song_id in ids:
        song = by_id.get(song_id)
        if song is None:
            continue
        entry = _song_entry(song)
        if entry is None:
            continue
        ordered.append(entry)
    return ordered


def fallback_goal_song_audio_urls(
    *,
    team: Team,
    season: Season | None,
) -> list[str]:
    """Resolve playable fallback audio belonging to the selected team roster."""
    ids = fallback_goal_song_song_ids(team=team, season=season)
    if not ids:
        return []

    roster_player_ids = main_roster_ids(team=team, season=season)
    songs = list(
        player_songs_by_ids(song_ids=ids).filter(player_id__in=roster_player_ids)
    )
    entries = song_entries_for_ids(songs=songs, ids=ids)
    audio_urls: list[str] = []
    for entry in entries:
        audio_url = entry.get("audio_url")
        if isinstance(audio_url, str):
            audio_urls.append(audio_url)
    return audio_urls
