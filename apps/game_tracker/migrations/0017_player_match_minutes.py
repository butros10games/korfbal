"""Add PlayerMatchMinutes table for persisted minutes-played."""

from __future__ import annotations

import bg_uuidv7.bg_uuidv7
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("game_tracker", "0016_player_match_impact_indexes"),
    ]

    operations = [
        migrations.CreateModel(
            name="PlayerMatchMinutes",
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
                    "minutes_played",
                    models.DecimalField(decimal_places=2, default="0.00", max_digits=6),
                ),
                (
                    "computed_at",
                    models.DateTimeField(auto_now=True),
                ),
                (
                    "match_data",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="player_minutes",
                        to="game_tracker.matchdata",
                    ),
                ),
                (
                    "player",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="match_minutes",
                        to="player.player",
                    ),
                ),
            ],
            options={
                "indexes": [
                    models.Index(fields=["match_data"], name="minutes_match_idx"),
                    models.Index(fields=["player"], name="minutes_player_idx"),
                    models.Index(
                        fields=["player", "algorithm_version", "match_data"],
                        name="minutes_pl_ver_md_idx",
                    ),
                ],
            },
        ),
        migrations.AddConstraint(
            model_name="playermatchminutes",
            constraint=models.UniqueConstraint(
                fields=("match_data", "player", "algorithm_version"),
                name="uniq_player_match_minutes",
            ),
        ),
    ]
