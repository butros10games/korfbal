"""Add indexes for Shot aggregations."""

from __future__ import annotations

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("game_tracker", "0012_playerchange_nullable_players"),
    ]

    operations = [
        migrations.AddIndex(
            model_name="shot",
            index=models.Index(
                fields=["match_data", "team", "scored"],
                name="game_tracke_match_d_7f4a4a_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="shot",
            index=models.Index(
                fields=["player", "scored"],
                name="game_tracke_player__e6d0d1_idx",
            ),
        ),
    ]
