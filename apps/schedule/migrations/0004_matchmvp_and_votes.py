"""Add Match MVP voting models."""

from __future__ import annotations

import bg_uuidv7
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("schedule", "0003_match_schedule_ma_start_t_7cfb51_idx_and_more"),
        ("player", "0002_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="MatchMvp",
            fields=[
                (
                    "id_uuid",
                    models.UUIDField(
                        default=bg_uuidv7.uuidv7,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("finished_at", models.DateTimeField()),
                ("closes_at", models.DateTimeField()),
                ("published_at", models.DateTimeField(blank=True, null=True)),
                (
                    "created_at",
                    models.DateTimeField(auto_now_add=True),
                ),
                (
                    "updated_at",
                    models.DateTimeField(auto_now=True),
                ),
                (
                    "match",
                    models.OneToOneField(
                        on_delete=models.deletion.CASCADE,
                        related_name="mvp",
                        to="schedule.match",
                    ),
                ),
                (
                    "mvp_player",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=models.deletion.SET_NULL,
                        related_name="mvp_awards",
                        to="player.player",
                    ),
                ),
            ],
            options={
                "indexes": [
                    models.Index(fields=["match"], name="schedule_ma_match_i_2ed106_idx"),
                    models.Index(fields=["closes_at"], name="schedule_ma_closes__b44233_idx"),
                    models.Index(fields=["published_at"], name="schedule_ma_publish_e5a6e8_idx"),
                ],
            },
        ),
        migrations.CreateModel(
            name="MatchMvpVote",
            fields=[
                (
                    "id_uuid",
                    models.UUIDField(
                        default=bg_uuidv7.uuidv7,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                (
                    "created_at",
                    models.DateTimeField(auto_now_add=True),
                ),
                (
                    "updated_at",
                    models.DateTimeField(auto_now=True),
                ),
                (
                    "candidate",
                    models.ForeignKey(
                        on_delete=models.deletion.CASCADE,
                        related_name="mvp_votes_received",
                        to="player.player",
                    ),
                ),
                (
                    "match",
                    models.ForeignKey(
                        on_delete=models.deletion.CASCADE,
                        related_name="mvp_votes",
                        to="schedule.match",
                    ),
                ),
                (
                    "voter",
                    models.ForeignKey(
                        on_delete=models.deletion.CASCADE,
                        related_name="mvp_votes_cast",
                        to="player.player",
                    ),
                ),
            ],
            options={
                "indexes": [
                    models.Index(fields=["match"], name="schedule_ma_match_i_5b6a08_idx"),
                    models.Index(fields=["candidate"], name="schedule_ma_candida_77f876_idx"),
                ],
            },
        ),
        migrations.AddConstraint(
            model_name="matchmvpvote",
            constraint=models.UniqueConstraint(
                fields=("match", "voter"),
                name="unique_mvp_vote_per_match_voter",
            ),
        ),
    ]
