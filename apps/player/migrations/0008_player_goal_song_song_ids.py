"""Add Player.goal_song_song_ids.

Stores a list of PlayerSong UUIDs that should be cycled through as goal songs.
"""

from __future__ import annotations

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("player", "0007_playersong"),
    ]

    operations = [
        migrations.AddField(
            model_name="player",
            name="goal_song_song_ids",
            field=models.JSONField(blank=True, default=list),
        ),
    ]
