"""Tests for stale CachedSong in-progress state recovery."""

from __future__ import annotations

from datetime import timedelta

from django.test import override_settings
from django.utils import timezone
import pytest

from apps.player.models.cached_song import CachedSong, CachedSongStatus
from apps.player.tasks import download_cached_song


@pytest.mark.django_db
@override_settings(
    TESTING=True,
    SPOTDL_DOWNLOAD_TIMEOUT_SECONDS=1,
    SPOTDL_STALE_IN_PROGRESS_SECONDS=1,
)
def test_download_cached_song_reclaims_stale_downloading() -> None:
    """A CachedSong stuck in DOWNLOADING should be reclaimed and completed."""
    cached = CachedSong.objects.create(
        spotify_url="https://open.spotify.com/track/27CXrzqx1N44o1Pi6AHRT4",
        status=CachedSongStatus.DOWNLOADING,
        error_message="",
    )

    # Force an old updated_at so the task considers this state stale.
    CachedSong.objects.filter(id_uuid=cached.id_uuid).update(
        updated_at=timezone.now() - timedelta(seconds=10)
    )

    download_cached_song.apply(args=[str(cached.id_uuid)])

    cached.refresh_from_db()
    assert cached.status == CachedSongStatus.READY
    assert cached.audio_file
    assert not cached.error_message
