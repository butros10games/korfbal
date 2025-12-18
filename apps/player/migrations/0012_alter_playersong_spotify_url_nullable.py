"""Make PlayerSong.spotify_url optional.

Uploaded MP3 songs don't have a Spotify source URL.
"""

from __future__ import annotations

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("player", "0011_player_privacy_visibility_fields"),
    ]

    operations = [
        migrations.AlterField(
            model_name="playersong",
            name="spotify_url",
            field=models.URLField(blank=True, max_length=500, null=True),
        ),
    ]
