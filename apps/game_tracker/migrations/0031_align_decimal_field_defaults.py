"""Align persisted score defaults with their Decimal model values."""

from __future__ import annotations

from decimal import Decimal

from django.db import migrations, models


class Migration(migrations.Migration):
    """Record Decimal defaults without changing stored values."""

    dependencies = [
        ("game_tracker", "0030_backfill_historical_event_snapshots"),
    ]

    operations = [
        migrations.RenameIndex(
            model_name="playerchange",
            new_name="playerchange_group_time_idx",
            old_name="playerchange_match_group_time_idx",
        ),
        migrations.AlterField(
            model_name="playermatchimpact",
            name="impact_score",
            field=models.DecimalField(
                decimal_places=1,
                default=Decimal("0.0"),
                max_digits=7,
            ),
        ),
        migrations.AlterField(
            model_name="playermatchminutes",
            name="minutes_played",
            field=models.DecimalField(
                decimal_places=2,
                default=Decimal("0.00"),
                max_digits=6,
            ),
        ),
    ]
