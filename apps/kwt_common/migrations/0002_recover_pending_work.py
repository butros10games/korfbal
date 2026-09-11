"""Adopt unfinished media and open MVP deadlines without contacting Celery."""

from datetime import timedelta
from typing import Any

from django.db import migrations
from django.utils import timezone


def recover(apps: Any, schema_editor: Any) -> None:
    """Seed durable work while preserving completed historical notifications."""
    jobs = apps.get_model("kwt_common", "BackgroundJob").objects.using(schema_editor.connection.alias)
    now = timezone.now()
    for model, task in (("CachedSong", "download_cached_song"), ("PlayerSong", "download_player_song")):
        songs = apps.get_model("player", model).objects.using(schema_editor.connection.alias)
        # Ready sources may still need a clip. Preparing an existing clip is cheap
        # and does not download the source again; failed jobs remain operator-owned.
        for song_id in songs.exclude(status="failed").values_list("pk", flat=True).iterator():
            name = f"apps.player.tasks.{task}"
            jobs.get_or_create(key=f"{name}:{song_id}", defaults={"task": name, "args": [str(song_id)], "queue": "media", "due_at": now})
    mvps = apps.get_model("awards", "MatchMvp").objects.using(schema_editor.connection.alias)
    for mvp in mvps.filter(published_at=None).iterator():
        for task, due in (("send_mvp_vote_reminder", mvp.closes_at - timedelta(hours=1)),
                          ("publish_mvp_and_notify", mvp.closes_at + timedelta(minutes=1))):
            if task == "send_mvp_vote_reminder" and mvp.closes_at <= now:
                continue
            name = f"apps.player.tasks.{task}"
            jobs.get_or_create(key=f"{name}:{mvp.match_id}", defaults={"task": name, "kwargs": {"match_id": str(mvp.match_id)}, "due_at": due})


class Migration(migrations.Migration):
    """Recover pending intents once; rollback does not retract completed work."""

    dependencies = [
        ("kwt_common", "0001_initial"),
        ("player", "0028_alter_player_profile_picture_and_more"),
        ("awards", "0002_remove_matchmvp_schedule_ma_match_i_2ed106_idx_and_more"),
    ]
    operations = [migrations.RunPython(recover, migrations.RunPython.noop)]
