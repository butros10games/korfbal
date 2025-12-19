"""Add PlayerMatchImpactBreakdown table for persisted breakdowns."""

from __future__ import annotations

import bg_uuidv7.bg_uuidv7
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("game_tracker", "0014_player_match_impact"),
    ]

    operations = [
        migrations.CreateModel(
            name="PlayerMatchImpactBreakdown",
            fields=[
                (
                    "id_uuid",
                    models.UUIDField(
                        default=bg_uuidv7.bg_uuidv7.uuidv7,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                (
                    "algorithm_version",
                    models.CharField(default="v1", max_length=32),
                ),
                (
                    "breakdown",
                    models.JSONField(default=dict),
                ),
                (
                    "computed_at",
                    models.DateTimeField(auto_now=True),
                ),
                (
                    "impact",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="breakdown",
                        to="game_tracker.playermatchimpact",
                    ),
                ),
            ],
            options={
                "indexes": [
                    models.Index(
                        fields=["algorithm_version"],
                        name="impact_bd_ver_idx",
                    )
                ],
            },
        ),
    ]
