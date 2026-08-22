"""Queue clip preparation for ready direct-upload goal songs."""

from __future__ import annotations

from typing import Any

from celery import current_app
from django.db import migrations


PREPARE_TASK_NAME = "apps.player.tasks.download_player_song"


def enqueue_goal_song_clip_preparation(apps: Any, _schema_editor: Any) -> None:
    """Hand existing direct-upload songs to the ffmpeg-enabled Celery worker."""
    PlayerSong = apps.get_model("player", "PlayerSong")
    song_ids = (
        PlayerSong.objects.filter(
            cached_song_id=None,
            status="ready",
        )
        .exclude(audio_file="")
        .values_list("id_uuid", flat=True)
        .iterator(chunk_size=500)
    )
    for song_id in song_ids:
        current_app.send_task(PREPARE_TASK_NAME, args=[str(song_id)])


class Migration(migrations.Migration):
    dependencies = [
        ("player", "0019_backfill_legacy_goal_songs"),
    ]

    operations = [
        migrations.RunPython(
            enqueue_goal_song_clip_preparation,
            migrations.RunPython.noop,
        ),
    ]
