"""Add historical Player↔Club memberships.

This introduces a time-bounded membership model so players can switch clubs
without rewriting historical data.
"""

from __future__ import annotations

import bg_uuidv7.bg_uuidv7
from django.db import migrations, models
from django.db.models import Q
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):
    dependencies = [
        ("club", "0004_alter_club_logo"),
        ("player", "0013_alter_playersong_spotify_url_not_null"),
    ]

    operations = [
        migrations.CreateModel(
            name="PlayerClubMembership",
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
                    "start_date",
                    models.DateField(default=django.utils.timezone.localdate),
                ),
                (
                    "end_date",
                    models.DateField(blank=True, null=True),
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
                    "club",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="player_membership_links",
                        to="club.club",
                    ),
                ),
                (
                    "player",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="club_membership_links",
                        to="player.player",
                    ),
                ),
            ],
            options={
                "indexes": [
                    models.Index(fields=["player"], name="pcm_player_idx"),
                    models.Index(fields=["club"], name="pcm_club_idx"),
                    models.Index(
                        fields=["player", "club"],
                        name="pcm_player_club_idx",
                    ),
                    models.Index(fields=["start_date"], name="pcm_start_date_idx"),
                    models.Index(fields=["end_date"], name="pcm_end_date_idx"),
                ],
                "constraints": [
                    models.CheckConstraint(
                        condition=Q(("end_date__isnull", True))
                        | Q(("end_date__gte", models.F("start_date"))),
                        name="playerclubmembership_end_after_start",
                    ),
                    models.UniqueConstraint(
                        condition=Q(("end_date__isnull", True)),
                        fields=("player", "club"),
                        name="playerclubmembership_unique_active",
                    ),
                ],
            },
        ),
        migrations.AddField(
            model_name="player",
            name="member_clubs",
            field=models.ManyToManyField(
                blank=True,
                related_name="members",
                through="player.PlayerClubMembership",
                to="club.club",
            ),
        ),
    ]
