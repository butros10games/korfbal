"""Make PlayerSong.spotify_url non-null.

Use an empty string instead of NULL for uploaded MP3 songs to satisfy Django
style guidelines (DJ001).
"""

from __future__ import annotations

from django.db import migrations, models


def _fill_null_spotify_urls(apps: object, schema_editor: object) -> None:  # noqa: ARG001
    PlayerSong = apps.get_model("player", "PlayerSong")
    PlayerSong.objects.filter(spotify_url__isnull=True).update(spotify_url="")


class Migration(migrations.Migration):
    dependencies = [
        ("player", "0012_alter_playersong_spotify_url_nullable"),
    ]

    operations = [
        migrations.RunPython(_fill_null_spotify_urls, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="playersong",
            name="spotify_url",
            field=models.URLField(blank=True, default="", max_length=500),
        ),
    ]
