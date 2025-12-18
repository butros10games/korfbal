"""Add PlayerMatchImpact table for persisted impact scores."""

from __future__ import annotations

import bg_uuidv7.bg_uuidv7
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("game_tracker", "0013_shot_indexes"),
        ("player", "0001_initial"),
        ("team", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="PlayerMatchImpact",
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
                    "impact_score",
                    models.DecimalField(
                        decimal_places=1, max_digits=7, default="0.0"
                    ),
                ),
                (
                    "algorithm_version",
                    models.CharField(default="v1", max_length=32),
                ),
                (
                    "computed_at",
                    models.DateTimeField(auto_now=True),
                ),
                (
                    "match_data",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="player_impacts",
                        to="game_tracker.matchdata",
                    ),
                ),
                (
                    "player",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="match_impacts",
                        to="player.player",
                    ),
                ),
                (
                    "team",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="player_match_impacts",
                        to="team.team",
                    ),
                ),
            ],
            options={
                "indexes": [
                    models.Index(fields=["match_data"], name="impact_match_idx"),
                    models.Index(fields=["team"], name="impact_team_idx"),
                    models.Index(fields=["player"], name="impact_player_idx"),
                ],
            },
        ),
        migrations.AddConstraint(
            model_name="playermatchimpact",
            constraint=models.UniqueConstraint(
                fields=("match_data", "player"),
                name="uniq_player_match_impact",
            ),
        ),
    ]
