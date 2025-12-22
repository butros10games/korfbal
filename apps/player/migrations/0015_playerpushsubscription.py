"""Add PlayerPushSubscription model."""

from __future__ import annotations

from typing import Any

from bg_uuidv7 import uuidv7
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("player", "0014_player_club_membership"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="PlayerPushSubscription",
            fields=[
                (
                    "id_uuid",
                    models.UUIDField(
                        default=uuidv7,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                (
                    "endpoint",
                    models.URLField(max_length=1024, unique=True),
                ),
                (
                    "subscription",
                    models.JSONField(),
                ),
                (
                    "is_active",
                    models.BooleanField(default=True),
                ),
                (
                    "user_agent",
                    models.TextField(blank=True, default=""),
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
                    "user",
                    models.ForeignKey(
                        on_delete=models.deletion.CASCADE,
                        related_name="push_subscriptions",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "indexes": [
                    models.Index(
                        fields=["user", "is_active"],
                        name="push_user_active_idx",
                    ),
                ],
            },
        ),
    ]
