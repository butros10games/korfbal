"""Durable media job adapter."""

from apps.kwt_common.services.jobs import enqueue


class CelerySongDownloadDispatcher:
    """Record media work in the caller transaction."""

    def cached_song(self, song_id: str) -> None:
        """Download a shared source and prepare its clips."""
        enqueue(
            "apps.player.tasks.download_cached_song",
            song_id,
            args=[song_id],
            queue="media",
        )

    def player_song(self, song_id: str) -> None:
        """Prepare player audio independently of broker availability."""
        enqueue(
            "apps.player.tasks.download_player_song",
            song_id,
            args=[song_id],
            queue="media",
        )
