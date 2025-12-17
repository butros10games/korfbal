"""Add privacy visibility fields to Player."""

from __future__ import annotations

from django.db import migrations, models


class Migration(migrations.Migration):
    """Add privacy visibility fields to Player."""

    dependencies = [
        ("player", "0010_playersong_playback_speed"),
    ]

    operations = [
        migrations.AddField(
            model_name="player",
            name="profile_picture_visibility",
            field=models.CharField(
                choices=[
                    ("public", "Public"),
                    ("club", "Club"),
                    ("private", "Private"),
                ],
                default="public",
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="player",
            name="stats_visibility",
            field=models.CharField(
                choices=[
                    ("public", "Public"),
                    ("club", "Club"),
                    ("private", "Private"),
                ],
                default="public",
                max_length=16,
            ),
        ),
    ]
