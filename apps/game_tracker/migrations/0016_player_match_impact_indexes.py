"""Add composite indexes for PlayerMatchImpact read patterns."""

from __future__ import annotations

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("game_tracker", "0015_player_match_impact_breakdown"),
    ]

    operations = [
        migrations.AddIndex(
            model_name="playermatchimpact",
            index=models.Index(
                fields=["player", "algorithm_version", "match_data"],
                name="impact_pl_ver_md_idx",
            ),
        ),
    ]
