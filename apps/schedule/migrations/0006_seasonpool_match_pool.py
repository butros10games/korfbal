"""Add season pools and optional pool assignment for matches."""

from __future__ import annotations

import bg_uuidv7.bg_uuidv7
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("schedule", "0005_matchmvpvote_anonymous_tokens"),
        ("team", "0005_teamdata_fallback_goal_song_song_ids"),
    ]

    operations = [
        migrations.CreateModel(
            name="SeasonPool",
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
                ("name", models.CharField(max_length=120)),
                (
                    "season",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="pools",
                        to="schedule.season",
                    ),
                ),
                (
                    "teams",
                    models.ManyToManyField(
                        blank=True,
                        related_name="season_pools",
                        to="team.team",
                    ),
                ),
            ],
            options={"ordering": ["name"]},
        ),
        migrations.AddField(
            model_name="match",
            name="pool",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="matches",
                to="schedule.seasonpool",
            ),
        ),
        migrations.AddIndex(
            model_name="match",
            index=models.Index(
                fields=["pool", "start_time"],
                name="schedule_ma_pool_id_db6846_idx",
            ),
        ),
        migrations.AddConstraint(
            model_name="seasonpool",
            constraint=models.UniqueConstraint(
                fields=("season", "name"),
                name="unique_pool_name_per_season",
            ),
        ),
    ]
