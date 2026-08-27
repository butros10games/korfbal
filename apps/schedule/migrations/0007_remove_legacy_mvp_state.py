"""Remove the old schedule-owned MVP models from migration state only."""

from __future__ import annotations

from django.db import migrations


class Migration(migrations.Migration):
    """Complete the state-only move to awards without dropping shared tables."""

    dependencies = [
        ("awards", "0001_initial"),
        ("schedule", "0006_seasonpool_match_pool"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[],
            state_operations=[
                migrations.RemoveField(
                    model_name="matchmvpvote",
                    name="candidate",
                ),
                migrations.RemoveField(
                    model_name="matchmvpvote",
                    name="match",
                ),
                migrations.RemoveField(
                    model_name="matchmvpvote",
                    name="voter",
                ),
                migrations.DeleteModel(name="MatchMvp"),
                migrations.DeleteModel(name="MatchMvpVote"),
            ],
        ),
    ]
