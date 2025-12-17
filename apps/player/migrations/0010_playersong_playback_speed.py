"""Add playback_speed to PlayerSong."""

from __future__ import annotations

from django.db import migrations, models


class Migration(migrations.Migration):
    """Add playback_speed to PlayerSong."""

    dependencies = [
        ("player", "0009_cached_song_cache"),
    ]

    operations = [
        migrations.AddField(
            model_name="playersong",
            name="playback_speed",
            field=models.FloatField(default=1.0),
        ),
    ]
