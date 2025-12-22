"""State-only migration to move MVP models into the awards app.

We intentionally do not create or rename any database tables.
The existing tables were created historically by schedule migrations:

- schedule_matchmvp
- schedule_matchmvpvote

This migration only updates Django's *state* so the models now belong to the
`awards` app label.
"""

from __future__ import annotations

import bg_uuidv7
from django.db import migrations, models
from django.db.models import Q


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        ("schedule", "0005_matchmvpvote_anonymous_tokens"),
        ("player", "0002_initial"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[],
            state_operations=[
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
                        ("created_at", models.DateTimeField(auto_now_add=True)),
                        ("updated_at", models.DateTimeField(auto_now=True)),
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
                        "db_table": "schedule_matchmvp",
                        "indexes": [
                            models.Index(
                                fields=["match"],
                                name="schedule_ma_match_i_2ed106_idx",
                            ),
                            models.Index(
                                fields=["closes_at"],
                                name="schedule_ma_closes__b44233_idx",
                            ),
                            models.Index(
                                fields=["published_at"],
                                name="schedule_ma_publish_e5a6e8_idx",
                            ),
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
                        ("voter_token", models.UUIDField(blank=True, null=True)),
                        ("created_at", models.DateTimeField(auto_now_add=True)),
                        ("updated_at", models.DateTimeField(auto_now=True)),
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
                                blank=True,
                                null=True,
                                on_delete=models.deletion.CASCADE,
                                related_name="mvp_votes_cast",
                                to="player.player",
                            ),
                        ),
                    ],
                    options={
                        "db_table": "schedule_matchmvpvote",
                        "indexes": [
                            models.Index(
                                fields=["match"],
                                name="schedule_ma_match_i_5b6a08_idx",
                            ),
                            models.Index(
                                fields=["candidate"],
                                name="schedule_ma_candida_77f876_idx",
                            ),
                            models.Index(
                                fields=["voter_token"],
                                name="schedule_mvpvote_token_idx",
                            ),
                        ],
                    },
                ),
                migrations.AddConstraint(
                    model_name="matchmvpvote",
                    constraint=models.UniqueConstraint(
                        fields=("match", "voter"),
                        condition=Q(voter__isnull=False),
                        name="unique_mvp_vote_per_match_voter",
                    ),
                ),
                migrations.AddConstraint(
                    model_name="matchmvpvote",
                    constraint=models.UniqueConstraint(
                        fields=("match", "voter_token"),
                        condition=Q(voter_token__isnull=False),
                        name="unique_mvp_vote_per_match_voter_token",
                    ),
                ),
                migrations.AddConstraint(
                    model_name="matchmvpvote",
                    constraint=models.CheckConstraint(
                        condition=(
                            (Q(voter__isnull=False) & Q(voter_token__isnull=True))
                            | (Q(voter__isnull=True) & Q(voter_token__isnull=False))
                        ),
                        name="mvp_vote_requires_voter_or_token",
                    ),
                ),
            ],
        ),
    ]
