"""Shared source download and independent player-clip preparation."""

from collections.abc import Callable
from pathlib import Path
import tempfile

from django.conf import settings
from django.core.files import File

from apps.player.models.cached_song import CachedSong
from apps.player.models.player_song import PlayerSong
from apps.player.services.player_song_queries import (
    player_song_by_id,
    player_song_queryset,
)


TrackDownloader = Callable[[str, Path], Path]
ClipPreparer = Callable[[PlayerSong], str | None]
CachedSongDispatcher = Callable[[str], None]


def _download_source(
    source: CachedSong | PlayerSong, download_track: TrackDownloader
) -> None:
    """Download once; durable job ownership handles retries and crashed workers.

    A stored source is reusable even when an earlier attempt failed afterward.
    Source and clip readiness are independent: failed clips never redownload audio.
    """
    if source.audio_file:
        if source.status != "ready":
            source.status, source.error_message = "ready", ""
            source.save(update_fields=["status", "error_message", "updated_at"])
        return
    try:
        source.status, source.error_message = "downloading", ""
        source.save(update_fields=["status", "error_message", "updated_at"])
        with tempfile.TemporaryDirectory(prefix="korfbal_audio_") as directory:
            output = Path(directory)
            if getattr(settings, "TESTING", False):
                downloaded = output / "dummy.mp3"
                downloaded.write_bytes(b"ID3")
            else:
                downloaded = download_track(source.spotify_url, output)
            source.status = "uploading"
            source.save(update_fields=["status", "updated_at"])
            with downloaded.open("rb") as handle:
                source.audio_file.save(
                    f"{source.pk}{downloaded.suffix or '.mp3'}",
                    File(handle),
                    save=False,
                )
        source.status, source.error_message = "ready", ""
        source.save(
            update_fields=["status", "error_message", "audio_file", "updated_at"]
        )
    except Exception as exc:
        source.status, source.error_message = "failed", type(exc).__name__
        source.save(update_fields=["status", "error_message", "updated_at"])
        raise


def process_cached_song_download(
    cached_song_id: str, *, download_track: TrackDownloader, prepare_clip: ClipPreparer
) -> None:
    """Store the shared source, then schedule its dependent clips."""
    cached = CachedSong.objects.filter(pk=cached_song_id).first()
    if cached is None:
        return
    _download_source(cached, download_track)
    for song in (
        player_song_queryset().filter(cached_song=cached).iterator(chunk_size=100)
    ):
        prepare_clip(song)


def process_player_song_download(
    song_id: str,
    *,
    dispatch_cached_song: CachedSongDispatcher,
    download_track: TrackDownloader,
    prepare_clip: ClipPreparer,
) -> None:
    """Prepare one clip, requesting its shared source first when necessary."""
    song = player_song_by_id(song_id)
    if song is None:
        return
    if song.cached_song is not None:
        if not song.cached_song.audio_file:
            dispatch_cached_song(str(song.cached_song_id))
            return
    else:
        _download_source(song, download_track)
    prepare_clip(song)
