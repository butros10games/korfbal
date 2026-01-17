"""Add indexes for tracker timeline queries."""

from __future__ import annotations

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("game_tracker", "0018_matchplayer_indexes"),
    ]

    operations = [
        migrations.AddIndex(
            model_name="shot",
            index=models.Index(
                fields=["match_data", "time"],
                name="shot_match_time_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="shot",
            index=models.Index(
                fields=["match_data", "scored", "time"],
                name="shot_match_scored_time_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="pause",
            index=models.Index(
                fields=["match_data", "active", "start_time"],
                name="pause_match_active_time_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="pause",
            index=models.Index(
                fields=["match_data", "start_time"],
                name="pause_match_time_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="playerchange",
            index=models.Index(
                fields=["match_data", "time"],
                name="playerchange_match_time_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="playerchange",
            index=models.Index(
                fields=["match_data", "player_group", "time"],
                name="playerchange_match_group_time_idx",
            ),
        ),
    ]
